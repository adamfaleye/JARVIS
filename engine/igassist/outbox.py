"""Outgoing messages: guards on AI text, the review/auto rules, and delivery with retry and backoff.

Nothing is reported as sent without Instagram's message id. Every message that doesn't go out says why.
"""
import json
import re
from datetime import datetime, timedelta

from . import kb, store, workflow

RETRY_DELAYS = [30, 120, 600, 1800]  # seconds
WINDOW_HOURS = 24                    # Instagram standard messaging window
MAX_PER_MINUTE = 20
MAX_BYTES = 1000                     # Instagram text limit (UTF-8 bytes)
AUTO_INTENTS = {"pricing", "services", "faq", "greeting", "thanks", "qualifying", "draft_ack", "consultation", "preparation",
                "aftercare", "hours", "location", "general", "escalation"}

_SAFE_AVAIL = [r"check(ing)? (his|her|their|my|the)? ?(calendar|schedule|availability)", r"confirm(ing)? (his|her|their|the)? ?availability",
               r"see (what|if) (he|she|they|mola) (has|have)", r"availability (with you|directly)", r"what works", r"works best",
               r"works for you", r"what day works", r"what time works"]
_AVAIL = [r"\b(is|are|'s|’s) (still )?(open|available|free)\b", r"\b(i|we|he|she|they|mola) (has|have|got) (an? |some )?(opening|spot|slot|availability|space)",
          r"\b(that|this|the) (time|day|date|slot) (works|is fine|is good|is open)", r"\bthere('s| is| are) (an? )?(opening|spot|slot|availability)",
          r"\bcan (fit|squeeze|get) you in\b", r"\bavailable (at|on|that|this|then)\b", r"\bopen (at|on|that|this)\b", r"\bplenty of (time|availability|openings)"]
_BOOKED = [r"\b(you're|you are|you've been|you have been|we've|we have|i've|i have|he's|he has|she's|she has)\s+(all\s+|now\s+)?(booked|scheduled|confirmed|set|locked in|reserved)\b",
           r"\b(appointment|booking|spot|slot|call|meeting|discovery call)\s+(is|has been)\s+(booked|confirmed|scheduled|set|reserved|locked)\b",
           r"\bbooked you\b", r"\bsee you (on|at|then|soon|next)\b", r"\byou're on (the|his|her|their|mola's) (calendar|schedule|books)\b",
           r"\bconfirm(ed|ing) (your|the) (appointment|booking)\b"]


def guard_problems(con, text, *, kind="reply", appointment=None):
    """Reasons this AI-written text can't be sent as is. Empty list = OK."""
    t = (text or "").strip()
    low = t.lower()
    probs = []
    if not t:
        return ["empty"]
    if len(t.encode("utf-8")) > MAX_BYTES:
        probs.append("too long for Instagram (1000 bytes)")
    if re.search(r"\[[^\]]*\]|\{[^}]*\}|<[^>]+>", t):
        probs.append("contains a placeholder")
    allowed = kb.approved_prices(con)
    for m in re.finditer(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)", t):
        cents = int(round(float(m.group(1).replace(",", "")) * 100))
        if cents not in allowed:
            probs.append(f"states a price that isn't in the price list ({m.group(0)})")
    if re.search(r"\b\d+\s*(dollars|bucks)\b", low):
        probs.append("states a price in words")
    if re.search(r"\b(discount|% off|percent off|coupon|promo code|on sale|special offer|deal for you)\b", low):
        probs.append("mentions a discount")
    durs = {int(s["duration_min"]) for s in kb.services(con).values() if s.get("duration_min")}
    for m in re.finditer(r"\b(\d+(?:\.\d+)?|an?|one|two|three|half an?)\s*(hours?|hrs?|minutes?|mins?)\b", low):
        num = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "half a": 0.5, "half an": 0.5}.get(m.group(1), None)
        num = float(m.group(1)) if num is None else num
        mins = num * 60 if m.group(2).startswith("h") else num
        if int(mins) not in durs and "24" not in m.group(0):
            probs.append(f"states a duration the owner hasn't provided ({m.group(0)})")
    for word, key in (("deposit", "deposit"), ("cancellation fee", "cancellation"), ("cancellation policy", "cancellation"),
                      ("late fee", "late_arrival"), ("no-show", "no_show"), ("no show", "no_show")):
        if word in low and not (kb.item(con, "policy", key) or {}).get("text"):
            probs.append(f"mentions a {word} policy the owner hasn't provided")
    scrub = low
    for p in _SAFE_AVAIL:
        scrub = re.sub(p, " ", scrub)
    for p in _AVAIL:
        if re.search(p, scrub):
            probs.append(kb.fmt(con, "claims availability (only {owner} can confirm availability)"))
            break
    booked = any(re.search(p, low) for p in _BOOKED)
    if booked and not (kind == "confirmation" and appointment is not None and workflow.may_tell_customer_booked(appointment)):
        probs.append(kb.fmt(con, "says it's booked or confirmed (only after {owner} confirms it in {cal})"))
    emojis = len(re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", t))
    voice = kb.item(con, "voice", "style") or {}
    if emojis > int(voice.get("max_emojis") if voice.get("max_emojis") is not None else 1):
        probs.append("too many emojis")
    if re.search(r"\b(as an ai|i am an ai|i'm an ai|language model)\b", low):
        probs.append("unnecessary AI disclaimer")
    return list(dict.fromkeys(probs))


def enqueue(con, conversation_id, text, *, author, kind="reply", intent=None, ai_meta=None, ai_original=None, auto_ok=False,
            reply_to=None, appointment_id=None):
    """author: 'ai' | 'owner' | 'operator' | 'system'. AI text is guarded; in review mode it waits as a draft."""
    text = (text or "").strip()
    status, hold = "queued", None
    if author == "ai":
        appt = con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone() if appointment_id else None
        probs = guard_problems(con, text, kind=kind, appointment=appt)
        if probs:
            status, hold = "blocked", "; ".join(probs)
        elif store.get(con, "reply_mode") != "auto" or not auto_ok or intent not in AUTO_INTENTS:
            status = "draft"
    if kind == "confirmation":
        appt = con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone() if appointment_id else None
        if not workflow.may_tell_customer_booked(appt):
            status, hold = "blocked", kb.fmt(con, "A confirmation can only go out after {owner} confirms it in {cal}.")
    meta = dict(ai_meta or {})
    if appointment_id:
        meta["appointment_id"] = appointment_id
    cur = con.execute("INSERT INTO messages(conversation_id,direction,author,text,status,hold_reason,intent,ai_meta,ai_original,created_at,next_attempt_at,reply_to,kind) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (conversation_id, "out", author, text, status, hold, intent, json.dumps(meta, default=str), ai_original,
                       store.now(), store.now(), reply_to, kind))
    con.commit()
    return cur.lastrowid


def approve_draft(con, message_id, user, text=None):
    """The owner sends an AI draft (optionally edited). Edited drafts count as AI corrections."""
    m = con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    if not m or m["direction"] != "out" or m["status"] not in ("draft", "blocked", "held", "failed"):
        raise ValueError("That message isn't waiting to be sent.")
    new = (text if text is not None else m["text"] or "").strip()
    if not new:
        raise ValueError("The message is empty.")
    if len(new.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Instagram allows up to 1000 characters per message.")
    meta = store.j(m["ai_meta"], {}) or {}
    if m["kind"] == "confirmation" or meta.get("appointment_id"):
        appt = con.execute("SELECT * FROM appointments WHERE id=?", (meta.get("appointment_id"),)).fetchone()
        if m["kind"] == "confirmation" and not workflow.may_tell_customer_booked(appt):
            raise ValueError(kb.fmt(con, "A confirmation can only go out after it's confirmed in {cal}."))
    if m["kind"] != "confirmation":
        _check_booked_words(con, m["conversation_id"], new)
    edited = new != (m["text"] or "")
    original = m["ai_original"] or (m["text"] if m["author"] == "ai" else None)
    author = "owner" if edited or m["author"] not in ("ai", "ai_reviewed") else "ai_reviewed"
    con.execute("UPDATE messages SET text=?, status='queued', hold_reason=NULL, error=NULL, attempts=0, next_attempt_at=?, ai_original=?, "
                "author=? WHERE id=?", (new, store.now(), original if m["author"] in ("ai", "ai_reviewed") or m["ai_original"] else None, author, message_id))
    store.audit(con, user["username"], "message.approve", message_id, {"edited": edited})
    con.commit()
    return edited


def _check_booked_words(con, conversation_id, text):
    """Nobody (the owner included) tells a customer they're booked by accident: allowed only when a request in this
    conversation is confirmed on the calendar."""
    if not any(re.search(p, text.lower()) for p in _BOOKED):
        return
    for a in con.execute("SELECT * FROM appointments WHERE conversation_id=?", (conversation_id,)):
        if workflow.may_tell_customer_booked(a):
            return
    raise ValueError(kb.fmt(con, "This message says it's booked, but nothing in this conversation is confirmed in {cal} yet. "
                                 "Confirm it in {cal} first (Appointment Requests), then send the confirmation."))


def send_manual(con, conversation_id, text, user):
    """The owner (or the operator) writes a reply themselves."""
    t = (text or "").strip()
    if not t:
        raise ValueError("The message is empty.")
    if len(t.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Instagram allows up to 1000 characters per message.")
    _check_booked_words(con, conversation_id, t)
    mid = enqueue(con, conversation_id, t, author="owner" if user["role"] == "owner" else "operator", kind="reply", intent="manual")
    store.audit(con, user["username"], "message.manual", mid)
    con.commit()
    return mid


def discard(con, message_id, user):
    n = con.execute("UPDATE messages SET status='discarded', hold_reason=? WHERE id=? AND direction='out' AND status IN ('draft','blocked','held','failed','queued')",
                    (f"Discarded by {user['username']}", message_id)).rowcount
    store.audit(con, user["username"], "message.discard", message_id)
    con.commit()
    return n == 1


def _window_open(conv):
    last = store.parse_ts(conv["last_inbound_at"])
    return bool(last and datetime.now(last.tzinfo) - last <= timedelta(hours=WINDOW_HOURS))


def deliver_due(con, transport, now=None):
    """Send what's due. Returns a summary dict. Safe to call repeatedly (idempotent per message)."""
    now = now or datetime.now().astimezone()
    out = {"sent": 0, "held": 0, "failed": 0, "retry": 0, "discarded": 0}
    rows = con.execute("SELECT m.*, c.mode, c.last_inbound_at, c.customer_id, cu.igsid FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                       "JOIN customers cu ON cu.id=c.customer_id WHERE m.status='queued' AND m.direction='out' ORDER BY m.id").fetchall()
    sent_recent = con.execute("SELECT COUNT(*) FROM messages WHERE status='sent' AND sent_at>=?",
                              ((now - timedelta(minutes=1)).isoformat(timespec="seconds"),)).fetchone()[0]
    paused = store.flag(con, "automation_paused")
    live = store.flag(con, "live_send")
    for m in rows:
        nxt = store.parse_ts(m["next_attempt_at"])
        if nxt and nxt > now:
            continue
        if m["author"] == "ai" and paused:
            continue  # stays queued; goes out after resume (or the owner discards it)
        if m["author"] == "ai" and m["mode"] == "human" and m["kind"] != "holding":
            con.execute("UPDATE messages SET status='discarded', hold_reason='The owner took over this conversation' WHERE id=?", (m["id"],))
            out["discarded"] += 1
            continue
        if m["kind"] == "confirmation":
            meta = store.j(m["ai_meta"], {}) or {}
            appt = con.execute("SELECT * FROM appointments WHERE id=?", (meta.get("appointment_id"),)).fetchone()
            if not workflow.may_tell_customer_booked(appt):
                con.execute("UPDATE messages SET status='blocked', hold_reason='Not confirmed in the calendar yet' WHERE id=?", (m["id"],))
                out["held"] += 1
                continue
        if str(m["igsid"]).startswith("test:"):
            ts = store.now()
            con.execute("UPDATE messages SET status='simulated', sent_at=?, hold_reason='Test conversation: shown here, never sent to Instagram.' WHERE id=?",
                        (ts, m["id"]))
            con.execute("UPDATE conversations SET last_outbound_at=?, intro_sent=CASE WHEN ? IN ('ai','ai_reviewed') THEN 1 ELSE intro_sent END WHERE id=?",
                        (ts, m["author"], m["conversation_id"]))
            out.setdefault("simulated", 0)
            out["simulated"] += 1
            continue
        if not live:
            con.execute("UPDATE messages SET status='held', hold_reason=? WHERE id=?",
                        ("Sending to Instagram is off (test mode). Nothing was sent.", m["id"]))
            out["held"] += 1
            continue
        if not _window_open(m):
            con.execute("UPDATE messages SET status='held', hold_reason=? WHERE id=?",
                        ("Outside Instagram's 24-hour reply window. Reply from the Instagram app.", m["id"]))
            out["held"] += 1
            continue
        if sent_recent >= MAX_PER_MINUTE:
            break
        con.execute("UPDATE messages SET status='sending', attempts=attempts+1 WHERE id=? AND status='queued'", (m["id"],))
        con.commit()
        try:
            mid = transport.send_text(m["igsid"], m["text"])
        except Exception as e:  # noqa: BLE001
            retryable = getattr(e, "retryable", True)
            attempts = m["attempts"] + 1
            if retryable and attempts <= len(RETRY_DELAYS):
                when = (now + timedelta(seconds=RETRY_DELAYS[attempts - 1])).isoformat(timespec="seconds")
                con.execute("UPDATE messages SET status='queued', error=?, next_attempt_at=? WHERE id=?", (str(e)[:500], when, m["id"]))
                out["retry"] += 1
            else:
                con.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (str(e)[:500], m["id"]))
                out["failed"] += 1
            store.health(con, "instagram_send", False, str(e)[:300])
            con.commit()
            continue
        if not mid:
            con.execute("UPDATE messages SET status='failed', error='Instagram returned no message id; not counted as sent' WHERE id=?", (m["id"],))
            out["failed"] += 1
            con.commit()
            continue
        ts = store.now()
        con.execute("UPDATE messages SET status='sent', sent_at=?, mid=?, error=NULL WHERE id=?", (ts, mid, m["id"]))
        con.execute("UPDATE conversations SET last_outbound_at=?, updated_at=?, intro_sent=CASE WHEN ? IN ('ai','ai_reviewed') THEN 1 ELSE intro_sent END WHERE id=?",
                    (ts, ts, m["author"], m["conversation_id"]))
        meta = store.j(m["ai_meta"], {}) or {}
        if m["kind"] == "confirmation" and meta.get("appointment_id"):
            con.execute("UPDATE appointments SET confirmation_message_id=? WHERE id=?", (m["id"], meta["appointment_id"]))
        store.health(con, "instagram_send", True, "last send ok")
        sent_recent += 1
        out["sent"] += 1
        con.commit()
    con.commit()
    return out
