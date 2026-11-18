"""Background work for one tenant: process webhook events, run the conversation engine, deliver due messages,
apply retention. Runs in the dashboard server's worker thread (python run.py assistant serve) or once via `assistant tick`.

The worker never approves, confirms or books anything: it only calls the automatic workflow moves and outbox.
"""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from . import brain, meta, outbox, store, workflow, kb

_lock = threading.Lock()


def process_events(con, tenant, transport=None, limit=50):
    done = 0
    rows = con.execute("SELECT * FROM webhook_events WHERE processed_at IS NULL AND signature_ok=1 AND attempts<5 ORDER BY id LIMIT ?", (limit,)).fetchall()
    for ev in rows:
        con.execute("UPDATE webhook_events SET attempts=attempts+1 WHERE id=?", (ev["id"],))
        con.commit()
        try:
            payload = json.loads(ev["body"])
            fetch = getattr(transport, "profile", None) if ev["source"] in ("meta", "poll") else None
            todo = meta.process_payload(con, payload, profile_fetch=fetch, is_test=1 if ev["source"] == "simulator" else 0)
            for conv_id, msg_id in todo:
                brain.handle(con, conv_id, msg_id)
            con.execute("UPDATE webhook_events SET processed_at=?, error=NULL WHERE id=?", (store.now(), ev["id"]))
            done += 1
        except Exception as e:  # noqa: BLE001  (kept for retry; error logged on the event)
            con.execute("UPDATE webhook_events SET error=? WHERE id=?", (f"{e.__class__.__name__}: {e}\n{traceback.format_exc()[-1500:]}", ev["id"]))
            store.health(con, "webhook_processing", False, f"{e.__class__.__name__}: {e}")
        con.commit()
    if rows:
        store.health(con, "webhook_processing", True, f"processed {done}")
    return done


def simulate_inbound(con, sender_label, text, attachments=None):
    """Test message from the dashboard: same pipeline as Meta, marked as test, never sent to Instagram."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Type a test message.")
    label = "".join(ch for ch in (sender_label or "tester").lower() if ch.isalnum() or ch in "_-")[:30] or "tester"
    igsid = f"test:{label}"
    now = datetime.now()
    mid = f"sim-{label}-{now.timestamp():.6f}"
    payload = {"object": "instagram", "entry": [{"id": "simulator", "time": int(now.timestamp()), "messaging": [
        {"sender": {"id": igsid}, "recipient": {"id": "simulator"}, "timestamp": int(now.timestamp() * 1000),
         "message": {"mid": mid, "text": t, "attachments": attachments or []}}]}]}
    con.execute("INSERT OR IGNORE INTO customers(igsid,username,created_at,updated_at,is_test) VALUES(?,?,?,?,1)", (igsid, label, store.now(), store.now()))
    con.commit()
    return meta.store_event(con, json.dumps(payload).encode(), True, source="simulator")


def apply_retention(con):
    days = store.get(con, "retention_days") or ""
    if not str(days).strip():
        return 0
    cutoff = (datetime.now() - timedelta(days=int(days))).astimezone().isoformat(timespec="seconds")
    n = con.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,)).rowcount
    con.execute("DELETE FROM webhook_events WHERE received_at < ?", (cutoff,))
    con.execute("DELETE FROM seen_events WHERE at < ?", (cutoff,))
    if n:
        store.audit(con, "system", "retention", "messages", {"deleted": n, "older_than_days": days})
    con.commit()
    return n


def delete_customer(con, customer_id, actor):
    """Remove one customer's data (deletion request). Appointment history goes too."""
    c = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c:
        return False
    conv = con.execute("SELECT id FROM conversations WHERE customer_id=?", (customer_id,)).fetchone()
    if conv:
        con.execute("DELETE FROM messages WHERE conversation_id=?", (conv["id"],))
        ids = [r["id"] for r in con.execute("SELECT id FROM appointments WHERE conversation_id=?", (conv["id"],))]
        for i in ids:
            con.execute("DELETE FROM appointment_events WHERE appointment_id=?", (i,))
            con.execute("DELETE FROM appointment_notes WHERE appointment_id=?", (i,))
        con.execute("DELETE FROM appointments WHERE conversation_id=?", (conv["id"],))
        con.execute("DELETE FROM conversations WHERE id=?", (conv["id"],))
    con.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    store.audit(con, actor, "customer.deleted", customer_id, {"username": c["username"]})
    con.commit()
    return True


def export_customer(con, customer_id):
    c = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c:
        return None
    conv = con.execute("SELECT * FROM conversations WHERE customer_id=?", (customer_id,)).fetchone()
    msgs = [dict(m) for m in con.execute("SELECT direction,author,text,status,created_at,sent_at FROM messages WHERE conversation_id=? ORDER BY id",
                                         (conv["id"] if conv else -1,))]
    appts = [workflow.detail(con, r["id"]) for r in con.execute("SELECT id FROM appointments WHERE customer_id=?", (customer_id,))]
    return {"customer": dict(c), "conversation": dict(conv) if conv else None, "messages": msgs, "appointments": appts}


POLL_SECONDS = 20
FIRST_LOOKBACK_HOURS = 3
_poll_state = {}


def _parse_ts(s):
    try:
        return datetime.strptime(str(s).replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def poll_inbox(con, tenant, transport, now=None):
    """Checks the Instagram inbox directly (Conversations API) and queues any new messages as if a webhook had
    delivered them. Meta often doesn't send messaging webhooks to apps that aren't published or reviewed; this makes
    the assistant work anyway. Duplicates with real webhooks are dropped by message id. Returns messages queued."""
    if not hasattr(transport, "recent_messages") or not store.flag(con, "inbox_poll"):
        return 0
    if not all(meta.configured(tenant).values()):
        return 0
    t = time.time() if now is None else now
    st = _poll_state.setdefault(tenant, {"next": 0})
    if t < st["next"]:
        return 0
    st["next"] = t + POLL_SECONDS
    since = _parse_ts(store.get(con, "poll_since") or "")
    if not since:  # first run: pick up the last few hours too, so a test DM sent just before setup isn't lost
        since = datetime.now(timezone.utc) - timedelta(hours=FIRST_LOOKBACK_HOURS)
        store.put(con, "poll_since", since.strftime("%Y-%m-%dT%H:%M:%S+0000"), "system")
    try:
        msgs = transport.recent_messages()
    except meta.SendError as e:
        st["next"] = t + (POLL_SECONDS if e.retryable else 300)
        store.health(con, "inbox_poll", False, str(e)[:300])
        con.commit()
        return 0
    account = str(getattr(transport, "account", "") or "")
    mine = transport.my_username() if hasattr(transport, "my_username") else ""
    events = []
    for m in sorted(msgs, key=lambda x: x["created"]):
        ts = _parse_ts(m["created"])
        if not m["mid"] or not ts or ts <= since:
            continue
        if con.execute("SELECT 1 FROM messages WHERE mid=?", (m["mid"],)).fetchone() or \
                con.execute("SELECT 1 FROM seen_events WHERE key=?", (f"mid:{m['mid']}",)).fetchone():
            continue
        ours = (account and m["from"] == account) or (mine and m["from_username"] == mine)
        base = {"timestamp": int(ts.timestamp() * 1000)}
        if ours:
            to = next((x for x in m["to"] if x and x != account), "")
            if not to:
                continue
            # our own API send shows up here too: skip it if the same text already went out in that conversation
            if con.execute("SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id JOIN customers u ON u.id=c.customer_id "
                           "WHERE u.igsid=? AND m.direction='out' AND m.text=?", (to, m["text"])).fetchone():
                continue
            events.append({**base, "sender": {"id": account}, "recipient": {"id": to},
                           "message": {"mid": m["mid"], "text": m["text"], "is_echo": True}})
        else:
            msg = {"mid": m["mid"], "text": m["text"]}
            if m["attachments"]:
                msg["attachments"] = m["attachments"]
            events.append({**base, "sender": {"id": m["from"]}, "recipient": {"id": account}, "message": msg})
    store.health(con, "inbox_poll", True, f"checked; {len(events)} new")
    if events:
        payload = {"object": "instagram", "entry": [{"id": account, "time": int(t), "messaging": events}]}
        meta.store_event(con, json.dumps(payload).encode(), True, source="poll")
    con.commit()
    return len(events)


def tick(con, tenant, transport):
    with _lock:
        poll_inbox(con, tenant, transport)
        n = process_events(con, tenant, transport)
        d = outbox.deliver_due(con, transport)
        r = 0
        if datetime.now().minute == 0:
            r = apply_retention(con)
        return {"events": n, "delivery": d, "retention": r}


def init_tenant(tenant):
    con = store.connect(tenant)
    added = kb.seed(con)
    return con, added
