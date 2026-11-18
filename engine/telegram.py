"""Telegram: Mola's phone line to his AI workforce.

Message the bot ("SDR: find 20 roofers in Lynn", "hunter, best leads today?", or just a question for JARVIS) and the
employee answers from the real records. The workforce also reports in: new replies, meetings booked, approvals
waiting, work it finished for you, anything that failed, and a morning summary. Quiet hours hold all but hot replies.

Security: the bot token lives only in .env on the PC (TELEGRAM_BOT_TOKEN). The bot serves exactly one chat: the
one that sent "/start <pairing code>" (the code shows in the JARVIS app). Everyone else is ignored. Approving from
the phone takes "approve <id>" in your own words, bound to the exact details you were shown.
"""
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime

from . import db, org

API = "https://api.telegram.org/bot{token}/{method}"
QUIET = ("21:00", "08:00")
MAX_LEN = 3900
HOT = ("interested", "meeting", "question")


def token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def call(method, payload=None, timeout=35, tok=None):
    tok = tok or token()
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN isn't set in .env")
    req = urllib.request.Request(API.format(token=tok, method=method), data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    if not out.get("ok"):
        raise RuntimeError(out.get("description") or "Telegram error")
    return out.get("result")


SEND = None  # tests replace this


def send(con, text, chat=None):
    chat = chat or db.kv_get(con, "tg_chat_id")
    if not chat:
        return False
    text = str(text or "").strip() or "(empty)"
    for i in range(0, len(text), MAX_LEN):
        part = text[i:i + MAX_LEN]
        (SEND or (lambda c, t: call("sendMessage", {"chat_id": c, "text": t, "disable_web_page_preview": True})))(chat, part)
    return True


def status(con):
    """For the app: is the phone line set up, and the pairing code if not paired yet."""
    paired = bool(db.kv_get(con, "tg_chat_id"))
    code = db.kv_get(con, "tg_pair_code")
    if not paired and not code:
        code = f"{random.randint(0, 999999):06d}"
        db.kv_set(con, "tg_pair_code", code); con.commit()
    return {"configured": bool(token()), "paired": paired, "pair_code": None if paired else code,
            "bot": db.kv_get(con, "tg_bot_name") or "", "quiet": QUIET, "muted": db.kv_get(con, "tg_muted") == "1",
            "last_poll": db.kv_get(con, "tg_last_poll") or "", "last_error": db.kv_get(con, "tg_last_error") or ""}


def unpair(con):
    db.kv_set(con, "tg_chat_id", ""); db.kv_set(con, "tg_pair_code", ""); con.commit()
    return status(con)


# ------------------------------------------------------------------ approvals from the phone
def _short(tid):
    return str(tid)[-5:]


def approvals(con, cfg):
    from . import hq
    rows = [dict(r) for r in con.execute("SELECT id, agent, title, params_hash, params FROM hq_tasks WHERE status='awaiting_approval' ORDER BY created LIMIT 10")]
    camps = [c for c in hq.campaigns(con)["campaigns"] if c["status"] == "review"]
    shown, lines = {}, []
    for r in rows:
        p = json.loads(r["params"] or "{}")
        detail = p.get("to") and f" to {p.get('to')}" or p.get("attendee") and f" with {p.get('attendee')}" or ""
        lines.append(f"{_short(r['id'])} · {org.ROSTER.get(r['agent'], {}).get('name', r['agent'])}: {r['title']}{detail}")
        shown[_short(r["id"])] = {"type": "task", "id": r["id"], "hash": r["params_hash"]}
    for c in camps[:5]:
        g = hq.campaign_get(con, c["id"], cfg)
        n = sum(1 for i in g.get("items") or [] if i.get("status") == "ready")
        lines.append(f"{_short(c['id'])} · Campaign \"{c['name']}\": {n} email(s) ready")
        shown[_short(c["id"])] = {"type": "campaign", "id": c["id"], "hash": g.get("items_hash")}
    db.kv_set(con, "tg_shown", json.dumps(shown)); con.commit()
    if not lines:
        return "Nothing is waiting for your approval."
    return "Waiting for your approval:\n" + "\n".join(lines) + "\n\nReply \"approve <id>\" or \"reject <id>\". Campaign emails can be read in the app first."


def decide(con, cfg, short, yes):
    from . import hq
    shown = json.loads(db.kv_get(con, "tg_shown") or "{}")
    it = shown.get(short)
    if not it:
        return "I don't have that id from the last list. Send \"approvals\" to get a fresh one."
    try:
        if it["type"] == "task":
            if yes:
                hq.approve(con, it["id"], it["hash"], by="owner (Telegram)")
                return "Approved. It runs on your PC shortly."
            hq.reject(con, it["id"], "rejected from Telegram", by="owner (Telegram)")
            return "Rejected."
        if yes:
            hq.campaign_approve(con, it["id"], it["hash"], cfg)
            return "Campaign approved. Emails go out paced from your Gmail."
        hq.campaign_set(con, it["id"], "pause")
        return "Campaign left paused."
    except ValueError as e:
        return f"Couldn't do that: {e}"


# ------------------------------------------------------------------ incoming messages
HELP = ("Talk to anyone on your team. Start with their name:\n"
        "  SDR: find 20 roofers in Lynn\n  AE: prep me for my call with Lakeview\n  Hunter, best leads today?\n"
        "Anything else goes to JARVIS, who hands it to the right person.\n\n"
        "approvals: what's waiting for you · approve <id> / reject <id>\n"
        "call / call sdr: a live voice call with an employee\n"
        "team: who's on the team · mute / unmute: pause updates · help")


def handle(con, cfg, text, talk=None):
    from . import employees
    t = str(text or "").strip()
    low = t.lower()
    if low in ("/help", "help", "/start"):
        return HELP
    if low in ("/team", "team"):
        return "\n".join(f"{m['name']}: {m['title']}" for m in employees.team())
    if low in ("/approvals", "approvals", "what needs me", "what's waiting"):
        return approvals(con, cfg)
    m = re.match(r"^(approve|reject)\s+([a-z0-9]{5})$", low)
    if m:
        return decide(con, cfg, m.group(2), m.group(1) == "approve")
    m = re.match(r"^/?call(?:\s+(.+))?$", low)
    if m:
        from . import voice_bridge
        aid = employees.resolve(m.group(1)) if m.group(1) else "jarvis"
        if aid not in voice_bridge.VOICE_AGENTS:
            return "Voice lines: " + ", ".join(org.ROSTER[a]["name"] for a in voice_bridge.VOICE_AGENTS)
        link = voice_bridge.call_link(aid)
        return f"Tap to call {org.ROSTER[aid]['name']} (keep this link private):\n{link}" if link else "Live calls aren't set up yet on your PC (README 9k)."
    if low in ("mute", "/mute"):
        db.kv_set(con, "tg_muted", "1"); con.commit()
        return "Updates paused. I'll still answer when you message. Send \"unmute\" to resume."
    if low in ("unmute", "/unmute"):
        db.kv_set(con, "tg_muted", "0"); db.kv_set(con, "tg_seen", json.dumps(_snapshot(con))); con.commit()  # no backlog flood
        return "Updates back on."
    aid, rest = employees.split_address(t)
    r = (talk or employees.talk)(con, cfg, aid or "jarvis", rest, source="owner_phone")
    return (f"{r['name']}: " if aid else "") + r["reply"]


def poll_once(con, cfg, talk=None, updates=None):
    """Read new messages and answer them. Returns how many were handled."""
    off = int(db.kv_get(con, "tg_offset") or 0)
    ups = updates if updates is not None else call("getUpdates", {"offset": off, "timeout": 25, "allowed_updates": ["message"]}, timeout=40)
    db.kv_set(con, "tg_last_poll", datetime.now().isoformat(timespec="seconds"))
    n = 0
    for u in ups or []:
        db.kv_set(con, "tg_offset", str(int(u["update_id"]) + 1)); con.commit()
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        text = msg.get("text") or ""
        if not chat or not text:
            continue
        mine = db.kv_get(con, "tg_chat_id")
        if not mine:
            code = db.kv_get(con, "tg_pair_code")
            if code and re.match(rf"^/start\s+{code}$", text.strip()):
                db.kv_set(con, "tg_chat_id", chat); db.kv_set(con, "tg_pair_code", "")
                db.kv_set(con, "tg_seen", json.dumps(_snapshot(con))); con.commit()
                send(con, "Paired. This is your private line to JARVIS and the team.\n\n" + HELP, chat)
            continue  # unpaired chats are ignored
        if chat != mine:
            continue  # someone else found the bot: ignore
        try:
            reply = handle(con, cfg, text, talk)
        except Exception as e:  # noqa: BLE001
            reply = f"Something went wrong: {e}"
        send(con, reply)
        n += 1
    return n


# ------------------------------------------------------------------ the team reporting in
def _snapshot(con):
    from . import hq
    reps = hq.email_replies(con, limit=50)["replies"]
    appr = [r[0] for r in con.execute("SELECT id FROM hq_tasks WHERE status='awaiting_approval'")]
    done = [r[0] for r in con.execute("SELECT id FROM hq_tasks WHERE source='owner_phone' AND status IN ('completed','failed') ORDER BY updated DESC LIMIT 100")]
    fail = [r[0] for r in con.execute("SELECT id FROM hq_tasks WHERE status='failed' ORDER BY updated DESC LIMIT 100")]
    camps = [c["id"] for c in hq.campaigns(con)["campaigns"] if c["status"] == "review"]
    meets = [r[0] for r in con.execute("SELECT id FROM hq_tasks WHERE kind='ae.book_meeting' AND status='completed' ORDER BY updated DESC LIMIT 50")]
    return {"replies": [str(r.get("id") or r.get("message_id") or r.get("at")) for r in reps], "approvals": appr, "done": done, "failed": fail,
            "campaigns": camps, "meetings": meets}


def _quiet(now=None):
    hm = (now or datetime.now()).strftime("%H:%M")
    return hm >= QUIET[0] or hm < QUIET[1]


def notify_tick(con, cfg, now=None):
    """What changed since the last look, sent as short updates. Returns the messages sent."""
    from . import hq
    if not db.kv_get(con, "tg_chat_id"):
        return []
    now = now or datetime.now()
    seen = json.loads(db.kv_get(con, "tg_seen") or "null")
    snap = _snapshot(con)
    if seen is None:
        db.kv_set(con, "tg_seen", json.dumps(snap)); con.commit()
        return []
    muted, quiet = db.kv_get(con, "tg_muted") == "1", _quiet(now)
    out = []
    reps = {str(r.get("id") or r.get("message_id") or r.get("at")): r for r in hq.email_replies(con, limit=50)["replies"]}
    for rid in snap["replies"]:
        if rid in seen.get("replies", []):
            continue
        r = reps.get(rid) or {}
        hot = (r.get("class") or "") in HOT
        if (muted or quiet) and not hot:
            continue
        out.append(f"SDR: new reply from {r.get('from_addr')}" + (f" ({r.get('class')})" if r.get("class") else "") + f":\n\"{str(r.get('text') or '')[:300]}\"")
    if not muted and not quiet:
        new_appr = [x for x in snap["approvals"] if x not in seen.get("approvals", [])]
        new_camp = [x for x in snap["campaigns"] if x not in seen.get("campaigns", [])]
        if new_appr or new_camp:
            out.append(f"{len(new_appr) + len(new_camp)} new item(s) waiting for your approval. Send \"approvals\" to see them.")
        for tid in snap["meetings"]:
            if tid not in seen.get("meetings", []):
                t = hq.get_task(con, tid, events=False)
                out.append(f"AE: meeting booked: {t['title']}")
        for tid in snap["done"]:
            if tid not in seen.get("done", []):
                t = hq.get_task(con, tid, events=False)
                who = org.ROSTER.get(t["agent"], {}).get("name", t["agent"])
                out.append(f"{who}: finished \"{t['title']}\"" + (f": {t['summary']}" if t.get("summary") else "") if t["status"] == "completed"
                           else f"{who}: couldn't finish \"{t['title']}\": {(t.get('error') or '')[:200]}")
        for tid in snap["failed"]:
            if tid not in seen.get("failed", []) and tid not in snap["done"]:
                t = hq.get_task(con, tid, events=False)
                out.append(f"{org.ROSTER.get(t['agent'], {}).get('name', t['agent'])}: \"{t['title']}\" failed: {(t.get('error') or '')[:200]}")
        day = now.strftime("%Y-%m-%d")
        if now.strftime("%H:%M") >= "08:30" and db.kv_get(con, "tg_brief_day") != day:
            db.kv_set(con, "tg_brief_day", day)
            try:
                mb = hq.morning_brief(con, cfg)
                out.append("Good morning. " + (mb.get("headline") or "Nothing needs you this morning."))
            except Exception:  # noqa: BLE001
                pass
    if muted or quiet:  # held: only hot replies went out; everything else stays unseen and goes out when quiet hours end
        sent = [rid for rid in snap["replies"] if rid not in seen.get("replies", []) and (reps.get(rid) or {}).get("class") in HOT]
        snap = dict(seen, replies=list(dict.fromkeys(seen.get("replies", []) + sent))[-300:])
    db.kv_set(con, "tg_seen", json.dumps(snap)); con.commit()
    for m in out[:8]:
        send(con, m)
    return out


def run_forever(cfg, log=print, stop=None):
    """The engine's phone line: answers messages and sends updates, until stopped."""
    from . import hq
    con = hq.connect()
    try:
        me = call("getMe", timeout=15)
        db.kv_set(con, "tg_bot_name", "@" + (me.get("username") or "")); con.commit()
        log(f"[{datetime.now():%H:%M}] Telegram: phone line on as @{me.get('username')}")
    except Exception as e:  # noqa: BLE001
        db.kv_set(con, "tg_last_error", str(e)[:200]); con.commit()
        log(f"[{datetime.now():%H:%M}] Telegram: couldn't start ({e})")
        return
    last_note = 0
    while not (stop and stop()):
        try:
            poll_once(con, cfg)
            if time.time() - last_note > 60:
                notify_tick(con, cfg); last_note = time.time()
            db.kv_set(con, "tg_last_error", ""); con.commit()
        except Exception as e:  # noqa: BLE001
            db.kv_set(con, "tg_last_error", str(e)[:200]); con.commit()
            time.sleep(10)
