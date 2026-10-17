"""Twilio follow-up sequences with compliance guardrails:
quiet hours, daily cap, DNC scrub, STOP handling, stop-on-reply, consent gate."""
import csv
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import db, config

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "remove", "wrong number", "do not text", "dont text"}


def e164(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None


def _client():
    from twilio.rest import Client
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _dnc(cfg):
    p = config.path(cfg["sms"].get("dnc_file", "data/dnc.txt"))
    if not p.exists():
        return set()
    return {e164(x) for x in p.read_text(encoding="utf-8").splitlines() if e164(x)}


def _add_dnc(cfg, phone):
    p = config.path(cfg["sms"].get("dnc_file", "data/dnc.txt"))
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(phone + "\n")
    try:  # suppression propagates to The Hunter's skip-traced contacts
        from . import hunter
        hunter.suppress_phone(hunter.connect(), phone, "opted out / Do Not Contact")
    except Exception:
        pass


def enroll(cfg, csv_path, consent=False, log=print):
    """CSV needs a phone column; optional first name / full name / property address columns.
    Adds people to the "Follow-up sequence" drip campaign (shown and controlled in JARVIS → Campaigns)."""
    from . import outreach
    con = outreach.connect()
    outreach.legacy_migrate(con, cfg)
    if not con.execute("SELECT 1 FROM campaigns WHERE id='cmp_followup'").fetchone():
        steps = [{"day": int(x.get("day", 0)), "text": x.get("text", "")} for x in cfg["sms"].get("sequence") or []]
        con.execute("INSERT INTO campaigns(id,name,kind,status,numbers,steps,created,updated,launched_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("cmp_followup", "Follow-up sequence", "drip", "running", "[]", json.dumps(steps), db.now(), db.now(), db.now(), "From config.yaml"))
        con.commit()
    rows, skipped = [], 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            phone = next((e164(v) for k, v in low.items() if "phone" in k or "mobile" in k or "cell" in k if e164(v)), None)
            if not phone:
                skipped += 1
                continue
            full = low.get("full name") or low.get("owner name") or low.get("name") or " ".join(x for x in [low.get("first name"), low.get("last name")] if x)
            rows.append({"phone": phone, "name": full, "first": low.get("first name") or "", "address": low.get("property address") or low.get("address") or "",
                         "source": "sms enroll"})
    res = outreach.add_audience(con, cfg, {"campaign_id": "cmp_followup", "contacts": rows, "attest_consent": bool(consent)})
    log(f"Enrolled {res['added']} contacts ({res['activated']} active, {res['blocked']} blocked, {skipped + res['invalid']} without a valid phone).")
    return res["added"]


def _in_quiet_ok(cfg, now):
    q = cfg["sms"].get("quiet_hours", {"start": "09:00", "end": "20:00"})
    return q["start"] <= now.strftime("%H:%M") < q["end"]


def render(text, c, cfg):
    first = (c["first_name"] or "").title() or "there"
    street = (c["address"] or "your property").split(",")[0]
    return text.format(first=first, street=street, sender=cfg["sms"].get("sender_name", ""))


def _crm_blocked(con):
    """Phones whose CRM deal is opted out or archived, plus anything under the app's emergency stop."""
    from . import crm
    blocked = set()
    for d in crm.items(con, "deals"):
        p = e164(d.get("phone"))
        if p and (d.get("optOut") or d.get("archived")):
            blocked.add(p)
    return blocked, crm.settings(con).get("emergencyStop")


def tick(cfg, log=print, client=None, now=None, pace=1.1):
    """Sends due campaign texts (blasts and drips). See engine/outreach.py for the guardrails."""
    from . import outreach
    con = outreach.connect()
    outreach.legacy_migrate(con, cfg)
    if not cfg["sms"].get("enabled"):
        return 0
    return outreach.tick(con, cfg, log=log, client=client, now_dt=now, pace=pace)


def poll(cfg, log=print, client=None):
    """Pulls inbound replies from Twilio. Replies stop the sequence and land in out/hot_replies.csv. STOP adds to DNC."""
    if not cfg["sms"].get("enabled"):
        return []
    con = db.connect()
    since = db.kv_get(con, "last_poll")
    client = client or _client()
    from . import outreach
    outreach.connect()
    kwargs = {"limit": 300}  # every number on the account
    if since:
        kwargs["date_sent_after"] = datetime.fromisoformat(since)
    from . import agent
    hot = []
    msgs = [m for m in client.messages.list(**kwargs) if getattr(m, "direction", "inbound") == "inbound"]
    msgs.sort(key=lambda m: getattr(m, "date_sent", None) or datetime.min)  # oldest first
    for m in msgs:
        phone, body = e164(m.from_), (m.body or "").strip()
        to = e164(getattr(m, "to", None))
        cur = con.execute("INSERT OR IGNORE INTO messages(phone,direction,body,sid,at,sender,status) VALUES(?,?,?,?,?,?,?)",
                          (phone, "in", body, m.sid, db.now(), to, "received"))
        if cur.rowcount == 0:
            continue
        con.commit()
        if body.lower().strip(" .!") in STOP_WORDS or any(w in body.lower() for w in ("stop texting", "wrong number", "do not text")):
            con.execute("UPDATE contacts SET status='stopped' WHERE phone=?", (phone,))
            _add_dnc(cfg, phone)
            outreach.on_inbound(con, phone, to, stop=True)
            log(f"  STOP from {phone}")
            try:
                agent._ensure_tables(con)
                agent.apply_actions(con, cfg, phone, {"status": "wrong_number" if "wrong number" in body.lower() else "opt_out",
                                                      "note": "", "reply": "", "follow_up_days": None}, body, log)
                agent.sync_thread(con, phone)
            except Exception as e:
                log(f"  (couldn't update JARVIS for {phone}: {e})")
        else:
            con.execute("UPDATE contacts SET status='replied' WHERE phone=?", (phone,))
            outreach.on_inbound(con, phone, to)
            c = con.execute("SELECT * FROM contacts WHERE phone=?", (phone,)).fetchone()
            hot.append({"phone": phone, "name": c["full_name"] if c else "", "address": c["address"] if c else "", "message": body})
            log(f"  REPLY from {phone}: {body[:80]}")
            try:
                agent.on_inbound(con, cfg, phone, body, cur.lastrowid, log)
            except Exception as e:
                log(f"  (texting agent error for {phone}: {e})")
    db.kv_set(con, "last_poll", datetime.utcnow().isoformat(timespec="seconds"))
    try:
        outreach.refresh_status(con, cfg, client)
    except Exception as e:
        log(f"  (delivery status check skipped: {e})")
    con.commit()
    if hot:
        p = config.path("out/hot_replies.csv")
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["received", "name", "phone", "address", "message"])
            if new:
                w.writeheader()
            for h in hot:
                w.writerow({"received": db.now(), **h})
    return hot


def overview():
    con = db.connect()
    st = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
    today = datetime.now().strftime("%Y-%m-%d")
    sent = con.execute("SELECT COUNT(*) FROM messages WHERE direction='out' AND at LIKE ?", (today + "%",)).fetchone()[0]
    replies = [dict(r) for r in con.execute(
        "SELECT m.phone, m.body, m.at, c.full_name, c.address FROM messages m LEFT JOIN contacts c ON c.phone=m.phone "
        "WHERE m.direction='in' ORDER BY m.id DESC LIMIT 20")]
    return {"contacts_by_status": st, "sent_today": sent, "recent_replies": replies}
