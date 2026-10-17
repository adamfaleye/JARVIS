"""Outreach: campaigns (one-time blasts and drips), sending numbers, click-to-call dialer, call log,
Do Not Contact, and Twilio usage. Everything sends through Mola's Twilio account.

Guardrails (same as the rest of the engine, never loosened from the app):
- Texting must be enabled in config.yaml (after A2P 10DLC approval) and Twilio set up in .env.
- Consent gate: a campaign only texts people with recorded permission, people who texted first, or Hunter
  contacts approved for automated outreach. A skip-traced or scraped number is not consent.
- Do Not Contact, opted-out or archived deals, and STOP replies always block. A STOP entry can't be removed here.
- Quiet hours, a global daily limit, a per-number daily limit, and at most one text per person every 20 hours.
- A reply stops every drip for that person. Launching is an explicit action in JARVIS (it asks first).
"""
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, crm, db, sms

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, status TEXT, numbers TEXT DEFAULT '[]', steps TEXT DEFAULT '[]',
  created TEXT, updated TEXT, launched_at TEXT, finished_at TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS enrollments(
  id INTEGER PRIMARY KEY, campaign_id TEXT, phone TEXT, name TEXT, first TEXT, address TEXT, city TEXT, deal_id TEXT,
  source TEXT, consent INTEGER DEFAULT 0, consent_note TEXT, step INTEGER DEFAULT 0, next_send TEXT,
  status TEXT DEFAULT 'pending', reason TEXT DEFAULT '', sender TEXT, enrolled_at TEXT, last_sent TEXT,
  UNIQUE(campaign_id, phone));
CREATE INDEX IF NOT EXISTS enr_due ON enrollments(status, next_send);
CREATE TABLE IF NOT EXISTS dnc_meta(phone TEXT PRIMARY KEY, source TEXT, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS sender_map(phone TEXT PRIMARY KEY, sender TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS dialer_calls(sid TEXT PRIMARY KEY, to_n TEXT, from_n TEXT, my_cell TEXT, deal_id TEXT, name TEXT, at TEXT);
"""
MSG_COLS = [("campaign_id", "TEXT"), ("sender", "TEXT"), ("status", "TEXT"), ("error_code", "TEXT"), ("step", "INTEGER")]
FINAL = {"delivered", "undelivered", "failed", "received", "read", "canceled"}
MERGE = {"first", "street", "city", "sender", "company"}
STATUS_LABEL = {"draft": "Draft", "running": "Running", "paused": "Paused", "done": "Finished", "cancelled": "Cancelled"}
DEFAULTS = {"myCell": "", "defaultNumber": "", "perNumberDaily": 100, "batch": 40, "callStart": "09:00", "callEnd": "20:00"}
GAP_HOURS = 20
_ERR = {"21610": "Recipient replied STOP (Twilio blocks it)", "21211": "Not a valid phone number", "21614": "Not a mobile number",
        "30003": "Unreachable handset", "30005": "Unknown or inactive number", "30006": "Landline or unreachable carrier",
        "30007": "Carrier filtered the message (check A2P registration and wording)", "30008": "Unknown carrier error",
        "30034": "Number isn't registered to an approved A2P campaign"}


# ---------------------------------------------------------------- setup
def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    for c, t in MSG_COLS:
        if c not in have:
            con.execute(f"ALTER TABLE messages ADD COLUMN {c} {t}")
    con.commit()
    return con


def now():
    return datetime.now().isoformat(timespec="seconds")


def _j(v):
    return json.dumps(v, default=str)


def settings(con, cfg=None):
    try:
        s = json.loads(db.kv_get(con, "out_settings") or "{}")
    except ValueError:
        s = {}
    out = dict(DEFAULTS, **{k: v for k, v in s.items() if k in DEFAULTS})
    if not out["defaultNumber"]:
        out["defaultNumber"] = sms.e164(os.environ.get("TWILIO_FROM")) or ""
    return out


def twilio_ready():
    if not os.environ.get("TWILIO_ACCOUNT_SID") or not os.environ.get("TWILIO_AUTH_TOKEN"):
        return False, "Twilio isn't set up yet: add TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN to .env on your PC, then fully restart Claude."
    return True, ""


def _client(client=None):
    if client is not None:
        return client
    ok, why = twilio_ready()
    if not ok:
        raise ValueError(why)
    return sms._client()


def segments(text):
    """SMS segment count: GSM-7 160/153, otherwise UCS-2 70/67."""
    gsm = re.fullmatch(r"[A-Za-z0-9 \r\n@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ!\"#¤%&'()*+,\-./:;<=>?¡ÄÖÑÜ§¿äöñüà^{}\\\[~\]|€]*", text or "")
    ext = sum(1 for ch in (text or "") if ch in "^{}\\[~]|€")
    n = len(text or "") + (ext if gsm else 0)
    if gsm:
        return 1 if n <= 160 else -(-n // 153)
    return 1 if n <= 70 else -(-n // 67)


# ---------------------------------------------------------------- Do Not Contact
def dnc_set(con, cfg):
    s = set(sms._dnc(cfg))
    s |= {r["phone"] for r in con.execute("SELECT phone FROM dnc_meta")}
    return s


def dnc_add(con, cfg, phone, source="manual", reason=""):
    p = sms.e164(phone)
    if not p:
        raise ValueError("That isn't a valid US phone number.")
    if p not in sms._dnc(cfg):
        sms._add_dnc(cfg, p)  # also suppresses the number in The Hunter
    con.execute("INSERT INTO dnc_meta(phone,source,reason,added_at) VALUES(?,?,?,?) ON CONFLICT(phone) DO NOTHING",
                (p, source, reason[:200], now()))
    con.execute("UPDATE enrollments SET status='stopped', reason='On Do Not Contact' WHERE phone=? AND status IN ('pending','active')", (p,))
    con.commit()
    return {"phone": p}


def dnc_list(con, cfg):
    meta = {r["phone"]: dict(r) for r in con.execute("SELECT * FROM dnc_meta")}
    stopped = {r["phone"] for r in con.execute("SELECT phone FROM contacts WHERE status='stopped'")}
    names = _names(con)
    out = []
    for p in sorted(dnc_set(con, cfg)):
        m = meta.get(p) or {}
        src = m.get("source") or ("stop" if p in stopped else "file")
        out.append({"phone": p, "source": src, "reason": m.get("reason") or ("Replied STOP" if src == "stop" else ""),
                    "added_at": m.get("added_at") or "", "name": names.get(p, ""), "removable": src == "manual"})
    return out


def dnc_remove(con, cfg, phone):
    p = sms.e164(phone)
    m = con.execute("SELECT * FROM dnc_meta WHERE phone=?", (p,)).fetchone()
    stopped = con.execute("SELECT 1 FROM contacts WHERE phone=? AND status='stopped'", (p,)).fetchone()
    if not m or m["source"] != "manual" or stopped:
        raise ValueError("Only numbers you added by hand can be removed. Someone who replied STOP has to text START to opt back in.")
    path = config.path(cfg["sms"].get("dnc_file", "data/dnc.txt"))
    if path.exists():
        keep = [x for x in path.read_text(encoding="utf-8").splitlines() if sms.e164(x) != p]
        path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    con.execute("DELETE FROM dnc_meta WHERE phone=?", (p,))
    con.commit()
    return {"phone": p}


# ---------------------------------------------------------------- eligibility
def _names(con):
    out = {}
    for r in con.execute("SELECT phone, full_name, first_name FROM contacts"):
        out[r["phone"]] = r["full_name"] or r["first_name"] or ""
    for d in crm.items(con, "deals"):
        p = sms.e164(d.get("phone"))
        if p and not out.get(p):
            out[p] = d.get("seller") or (d.get("address") or "").split(",")[0]
    for r in crm.items(con, "realtors"):
        p = sms.e164(r.get("phone"))
        if p and not out.get(p):
            out[p] = r.get("name") or ""
    return out


def _hunter_flags(con, phone):
    try:
        r = con.execute("SELECT MAX(auto_approved) a, MAX(manual_allowed) m, MAX(suppressed) s, COUNT(*) n FROM contact_points WHERE phone=?",
                        (phone,)).fetchone()
        return {"auto": bool(r["a"]), "manual": bool(r["m"]), "suppressed": bool(r["s"]), "hunter": bool(r["n"])}
    except Exception:
        return {"auto": False, "manual": False, "suppressed": False, "hunter": False}


def block_reason(con, cfg, phone, consent=False, dnc=None, crm_block=None):
    """Why this number can't get a campaign text right now ('' = eligible)."""
    p = sms.e164(phone)
    if not p:
        return "Not a valid US phone number"
    dnc = dnc if dnc is not None else dnc_set(con, cfg)
    if p in dnc:
        return "On Do Not Contact"
    if crm_block is None:
        crm_block, _ = sms._crm_blocked(con)
    if p in crm_block:
        return "Deal is opted out or archived"
    c = con.execute("SELECT consent, status FROM contacts WHERE phone=?", (p,)).fetchone()
    if c and c["status"] == "stopped":
        return "Asked to stop"
    hf = _hunter_flags(con, p)
    if hf["suppressed"]:
        return "Suppressed in The Hunter"
    if cfg["sms"].get("require_consent", True):
        texted_first = con.execute("SELECT 1 FROM messages WHERE phone=? AND direction='in' LIMIT 1", (p,)).fetchone()
        if not (consent or (c and c["consent"]) or texted_first or hf["auto"]):
            return "No recorded permission to text (a skip-traced number isn't consent)"
    return ""


# ---------------------------------------------------------------- campaigns
def _validate_steps(kind, steps):
    if not isinstance(steps, list) or not steps:
        raise ValueError("Add at least one message.")
    if kind == "blast":
        steps = steps[:1]
    out = []
    for i, s in enumerate(steps):
        text = str((s or {}).get("text") or "").strip()
        if not text:
            raise ValueError(f"Message {i + 1} is empty.")
        if len(text) > 480:
            raise ValueError(f"Message {i + 1} is over 480 characters.")
        bad = set(re.findall(r"\{(\w*)\}", text)) - MERGE
        if bad:
            raise ValueError(f"Message {i + 1} uses unknown merge field(s): {', '.join('{' + b + '}' for b in bad)}. Use {{first}}, {{street}}, {{city}}, {{sender}} or {{company}}.")
        day = 0 if kind == "blast" else int((s or {}).get("day") or 0)
        if out and day <= out[-1]["day"]:
            raise ValueError(f"Message {i + 1} has to come at least a day after message {i}.")
        out.append({"day": day, "text": text})
    if "stop" not in out[0]["text"].lower():
        raise ValueError("The first message needs opt-out wording, for example \"Reply STOP to opt out.\"")
    return out


def _row(r):
    d = dict(r)
    d["numbers"] = json.loads(d.get("numbers") or "[]")
    d["steps"] = json.loads(d.get("steps") or "[]")
    d["status_label"] = STATUS_LABEL.get(d["status"], d["status"])
    return d


def save_campaign(con, cfg, a):
    kind = a.get("kind") if a.get("kind") in ("blast", "drip") else "drip"
    name = str(a.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Give the campaign a name.")
    steps = _validate_steps(kind, a.get("steps"))
    numbers = [sms.e164(n) for n in (a.get("numbers") or []) if sms.e164(n)]
    cid = a.get("id")
    if cid:
        cur = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not cur:
            raise ValueError("Campaign not found.")
        if cur["status"] in ("done", "cancelled"):
            raise ValueError("This campaign is finished. Duplicate it to run it again.")
        if cur["status"] != "draft" and kind != cur["kind"]:
            raise ValueError("A launched campaign can't change type.")
        con.execute("UPDATE campaigns SET name=?, kind=?, steps=?, numbers=?, notes=?, updated=? WHERE id=?",
                    (name, kind, _j(steps), _j(numbers), str(a.get("notes") or "")[:500], now(), cid))
    else:
        cid = "cmp_" + uuid.uuid4().hex[:10]
        con.execute("INSERT INTO campaigns(id,name,kind,status,numbers,steps,created,updated,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                    (cid, name, kind, "draft", _j(numbers), _j(steps), now(), now(), str(a.get("notes") or "")[:500]))
    con.commit()
    return get_campaign(con, cfg, cid, enrollments=False)


def _first(name):
    n = str(name or "").strip()
    if not n or re.search(r"\b(llc|inc|trust|estate|corp|bank|company|co\.?)\b", n, re.I):
        return ""
    if "," in n:  # "SMITH, JOHN"
        n = n.split(",", 1)[1]
    return n.split()[0].title() if n.split() else ""


def _activate(con, cfg, cid, dnc=None, crm_block=None, at=None):
    """Checks every pending enrollment and makes it active or blocked."""
    dnc = dnc if dnc is not None else dnc_set(con, cfg)
    if crm_block is None:
        crm_block, _ = sms._crm_blocked(con)
    at = at or now()
    c = con.execute("SELECT steps FROM campaigns WHERE id=?", (cid,)).fetchone()
    first_day = int((json.loads(c["steps"]) if c else [{}])[0].get("day") or 0)
    first_send = (datetime.fromisoformat(at) + timedelta(days=first_day)).isoformat(timespec="seconds")
    act = blk = 0
    for e in con.execute("SELECT * FROM enrollments WHERE campaign_id=? AND status='pending'", (cid,)).fetchall():
        why = block_reason(con, cfg, e["phone"], bool(e["consent"]), dnc, crm_block)
        if why:
            con.execute("UPDATE enrollments SET status='blocked', reason=? WHERE id=?", (why, e["id"]))
            blk += 1
        else:
            con.execute("UPDATE enrollments SET status='active', reason='', next_send=?, enrolled_at=? WHERE id=?", (first_send, at, e["id"]))
            act += 1
    return act, blk


def add_audience(con, cfg, a):
    cid = a.get("campaign_id")
    c = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("Campaign not found.")
    if c["status"] in ("done", "cancelled"):
        raise ValueError("This campaign is finished.")
    rows = list(a.get("contacts") or [])
    if a.get("source") == "hunter":
        rows += hunter_audience(con, a.get("filters") or {})
    attest = bool(a.get("attest_consent"))
    added = dup = bad = 0
    for r in rows[:5000]:
        p = sms.e164(r.get("phone"))
        if not p:
            bad += 1
            continue
        name = str(r.get("name") or "")[:120]
        consent = 1 if (r.get("consent") or attest) else 0
        note = str(r.get("consent_note") or ("You confirmed permission when adding this list" if attest and not r.get("consent") else ""))[:200]
        cur = con.execute("""INSERT OR IGNORE INTO enrollments(campaign_id,phone,name,first,address,city,deal_id,source,consent,consent_note,status,enrolled_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                          (cid, p, name, str(r.get("first") or _first(name))[:40], str(r.get("address") or "")[:200],
                           str(r.get("city") or "")[:60], r.get("deal_id"), str(r.get("source") or a.get("source") or "list")[:30],
                           consent, note, now()))
        if cur.rowcount:
            added += 1
            if consent:
                con.execute("""INSERT INTO contacts(phone,first_name,full_name,address,consent,status) VALUES(?,?,?,?,1,'idle')
                               ON CONFLICT(phone) DO UPDATE SET consent=1""", (p, _first(name), name, str(r.get("address") or "")))
        else:
            dup += 1
    act = blk = 0
    if c["status"] in ("running", "paused"):
        act, blk = _activate(con, cfg, cid)
    con.execute("UPDATE campaigns SET updated=? WHERE id=?", (now(), cid))
    con.commit()
    out = {"added": added, "duplicates": dup, "invalid": bad, "activated": act, "blocked": blk}
    out.update(preview(con, cfg, cid))
    return out


def remove_audience(con, cfg, a):
    cid, phones = a.get("campaign_id"), [sms.e164(p) for p in a.get("phones") or []]
    n = 0
    for p in phones:
        if p:
            n += con.execute("UPDATE enrollments SET status='removed', reason='Removed by you' WHERE campaign_id=? AND phone=? AND status IN ('pending','active','blocked')",
                             (cid, p)).rowcount
    con.commit()
    return {"removed": n}


def hunter_audience(con, f):
    """Hunter contacts for a campaign. Only numbers approved for automated outreach count as permission."""
    q = """SELECT c.phone, c.name, p.street, p.city, p.county, s.score, s.tier, c.auto_approved
           FROM contact_points c JOIN properties p ON p.id=c.property_id LEFT JOIN lead_scores s ON s.property_id=p.id
           WHERE c.suppressed=0 AND c.phone<>''"""
    args = []
    if f.get("county"):
        q += " AND p.county=?"
        args.append(f["county"])
    if f.get("tiers"):
        q += f" AND s.tier IN ({','.join('?' * len(f['tiers']))})"
        args += list(f["tiers"])
    if f.get("min_score"):
        q += " AND s.score>=?"
        args.append(int(f["min_score"]))
    try:
        rows = con.execute(q + " LIMIT 5000", args).fetchall()
    except Exception:
        return []
    return [{"phone": r["phone"], "name": r["name"] or "", "address": r["street"] or "", "city": r["city"] or "",
             "consent": bool(r["auto_approved"]), "source": "hunter",
             "consent_note": "Approved for automated outreach in The Hunter" if r["auto_approved"] else ""} for r in rows]


def preview(con, cfg, cid):
    """Who would be texted if this campaign launched now, and why the rest wouldn't."""
    dnc, (crm_block, _) = dnc_set(con, cfg), sms._crm_blocked(con)
    reasons, eligible = {}, 0
    for e in con.execute("SELECT * FROM enrollments WHERE campaign_id=? AND status IN ('pending','active','blocked')", (cid,)):
        why = block_reason(con, cfg, e["phone"], bool(e["consent"]), dnc, crm_block)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
        else:
            eligible += 1
    c = con.execute("SELECT steps FROM campaigns WHERE id=?", (cid,)).fetchone()
    steps = json.loads(c["steps"]) if c else []
    return {"eligible": eligible, "excluded": sum(reasons.values()), "reasons": reasons,
            "segments": sum(segments(s["text"]) for s in steps), "texts_max": eligible * len(steps)}


def launch(con, cfg, cid, client=None, now_dt=None):
    c = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("Campaign not found.")
    if c["status"] != "draft":
        raise ValueError("This campaign has already been launched.")
    gate = send_gate(con, cfg)
    if gate and gate != "Outside texting hours.":
        raise ValueError(gate)
    _validate_steps(c["kind"], json.loads(c["steps"]))
    pv = preview(con, cfg, cid)
    if not pv["eligible"]:
        raise ValueError("Nobody on this list can be texted yet. " + "; ".join(f"{v} {k.lower()}" for k, v in pv["reasons"].items()))
    act, blk = _activate(con, cfg, cid, at=now_dt.replace(tzinfo=None).isoformat(timespec="seconds") if now_dt else None)
    con.execute("UPDATE campaigns SET status='running', launched_at=?, updated=? WHERE id=?", (now(), now(), cid))
    con.commit()
    return {"activated": act, "blocked": blk, "note": "Sending starts within a couple of minutes on your PC" +
            (" once texting hours begin." if gate else ".")}


def set_status(con, cid, status):
    c = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("Campaign not found.")
    allowed = {"paused": ("running",), "running": ("paused",), "cancelled": ("draft", "running", "paused")}
    if c["status"] not in allowed.get(status, ()):
        raise ValueError(f"Can't change a {STATUS_LABEL.get(c['status'], c['status']).lower()} campaign to {STATUS_LABEL.get(status, status).lower()}.")
    con.execute("UPDATE campaigns SET status=?, updated=?" + (", finished_at=?" if status == "cancelled" else "") + " WHERE id=?",
                (status, now(), now(), cid) if status == "cancelled" else (status, now(), cid))
    if status == "cancelled":
        con.execute("UPDATE enrollments SET status='cancelled' WHERE campaign_id=? AND status IN ('pending','active')", (cid,))
    con.commit()
    return {"status": status}


def duplicate(con, cid):
    c = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("Campaign not found.")
    nid = "cmp_" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO campaigns(id,name,kind,status,numbers,steps,created,updated,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                (nid, (c["name"] + " (copy)")[:80], c["kind"], "draft", c["numbers"], c["steps"], now(), now(), c["notes"] or ""))
    con.commit()
    return {"id": nid}


def _stats(con, cid):
    st = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM enrollments WHERE campaign_id=? GROUP BY status", (cid,))}
    m = {r["status"] or "sent": r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM messages WHERE campaign_id=? AND direction='out' GROUP BY status", (cid,))}
    sent = sum(m.values())
    contacted = con.execute("SELECT COUNT(DISTINCT phone) FROM messages WHERE campaign_id=? AND direction='out'", (cid,)).fetchone()[0]
    replied = st.get("replied", 0)
    return {"enrolled": sum(v for k, v in st.items() if k != "removed"), "active": st.get("active", 0), "pending": st.get("pending", 0),
            "blocked": st.get("blocked", 0), "done": st.get("done", 0), "replied": replied, "stopped": st.get("stopped", 0),
            "failed_people": st.get("failed", 0), "sent": sent, "delivered": m.get("delivered", 0) + m.get("read", 0),
            "failed": m.get("failed", 0) + m.get("undelivered", 0), "contacted": contacted,
            "response_rate": round(100 * replied / contacted, 1) if contacted else 0.0}


def list_campaigns(con, cfg):
    out = []
    for r in con.execute("SELECT * FROM campaigns ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 WHEN 'draft' THEN 2 ELSE 3 END, updated DESC"):
        d = _row(r)
        d["stats"] = _stats(con, d["id"])
        d["next_send"] = con.execute("SELECT MIN(next_send) FROM enrollments WHERE campaign_id=? AND status='active'", (d["id"],)).fetchone()[0]
        out.append(d)
    return out


def get_campaign(con, cfg, cid, enrollments=True):
    r = con.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ValueError("Campaign not found.")
    d = _row(r)
    d["stats"] = _stats(con, cid)
    d["segments"] = [segments(s["text"]) for s in d["steps"]]
    if enrollments:
        d["people"] = [dict(e) for e in con.execute(
            "SELECT phone,name,address,source,consent,consent_note,step,next_send,status,reason,sender,last_sent FROM enrollments WHERE campaign_id=? AND status<>'removed' ORDER BY id LIMIT 1000",
            (cid,))]
        if d["status"] == "draft":
            d["preview"] = preview(con, cfg, cid)
    return d


# ---------------------------------------------------------------- sending
def send_gate(con, cfg, now_dt=None):
    s = cfg.get("sms") or {}
    if not s.get("enabled"):
        return "Texting is off in config.yaml. Turn it on after your Twilio A2P registration is approved."
    ok, why = twilio_ready()
    if not ok:
        return why
    if not settings(con)["defaultNumber"] and not owned_numbers(con, cached_only=True):
        return "No sending number: add TWILIO_FROM to .env or pick a default number in JARVIS → Numbers."
    if crm.settings(con).get("emergencyStop"):
        return "Emergency stop is on in JARVIS."
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    if not sms._in_quiet_ok(cfg, now_dt or datetime.now(tz)):
        return "Outside texting hours."
    return ""


def render(text, e, cfg):
    first = (e["first"] or "").title() or "there"
    street = (e["address"] or "your property").split(",")[0].strip()
    return re.sub(r"\{(\w+)\}", lambda m: {"first": first, "street": street, "city": (e["city"] or "").title() or "your area",
                                           "sender": cfg["sms"].get("sender_name", ""), "company": "MolaBucks Capital Group"}.get(m.group(1), ""), text)


def _sent_today(con, day, sender=None):
    q, args = "SELECT COUNT(*) FROM messages WHERE direction='out' AND at LIKE ?", [day + "%"]
    if sender:
        q += " AND sender=?"
        args.append(sender)
    return con.execute(q, args).fetchone()[0]


def pick_sender(con, camp_numbers, phone, day, cap):
    """(number, why). Keeps each person on the number they already know; otherwise the least-used number."""
    owned = [n["phone"] for n in owned_numbers(con, cached_only=True)] or []
    sticky = con.execute("SELECT sender FROM sender_map WHERE phone=?", (phone,)).fetchone()
    dflt = settings(con)["defaultNumber"]
    pool = [n for n in (camp_numbers or []) if not owned or n in owned] or ([dflt] if dflt else owned[:1])
    if sticky and sticky["sender"] and (not owned or sticky["sender"] in owned):
        return (sticky["sender"], "") if _sent_today(con, day, sticky["sender"]) < cap else (None, "person")
    room = [(n, _sent_today(con, day, n)) for n in pool if n]
    room = [x for x in room if x[1] < cap]
    return (min(room, key=lambda x: x[1])[0], "") if room else (None, "all")


def remember_sender(con, phone, sender):
    if phone and sender:
        con.execute("INSERT INTO sender_map(phone,sender,updated) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET sender=excluded.sender, updated=excluded.updated",
                    (phone, sender, now()))


def tick(con, cfg, log=print, client=None, now_dt=None, pace=1.1):
    """Sends due campaign texts. Returns how many went out."""
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    now_dt = now_dt or datetime.now(tz)
    if not con.execute("SELECT 1 FROM campaigns WHERE status='running' LIMIT 1").fetchone():
        return 0
    gate = send_gate(con, cfg, now_dt)
    if gate:
        return 0
    st = settings(con)
    naive = now_dt.replace(tzinfo=None)
    day = naive.strftime("%Y-%m-%d")
    room = int(cfg["sms"].get("daily_limit", 150)) - _sent_today(con, day)
    if room <= 0:
        return 0
    batch = min(room, int(st["batch"] or 40))
    due = con.execute("""SELECT e.*, c.steps AS c_steps, c.numbers AS c_numbers, c.kind AS c_kind FROM enrollments e
                         JOIN campaigns c ON c.id=e.campaign_id
                         WHERE c.status='running' AND e.status='active' AND e.next_send<=? ORDER BY e.next_send LIMIT ?""",
                      (naive.isoformat(timespec="seconds"), batch * 3)).fetchall()
    if not due:
        _finish_campaigns(con)
        return 0
    dnc, (crm_block, _) = dnc_set(con, cfg), sms._crm_blocked(con)
    cl, sent = None, 0
    for e in due:
        if sent >= batch:
            break
        steps = json.loads(e["c_steps"])
        why = block_reason(con, cfg, e["phone"], bool(e["consent"]), dnc, crm_block)
        if why:
            con.execute("UPDATE enrollments SET status=?, reason=? WHERE id=?",
                        ("stopped" if why in ("On Do Not Contact", "Asked to stop") else "blocked", why, e["id"]))
            continue
        if e["step"] >= len(steps):
            con.execute("UPDATE enrollments SET status='done' WHERE id=?", (e["id"],))
            continue
        recent = con.execute("SELECT at FROM messages WHERE phone=? AND direction='out' ORDER BY id DESC LIMIT 1", (e["phone"],)).fetchone()
        if recent and recent["at"] and datetime.fromisoformat(recent["at"]) > naive - timedelta(hours=GAP_HOURS):
            later = datetime.fromisoformat(recent["at"]) + timedelta(hours=GAP_HOURS)
            con.execute("UPDATE enrollments SET next_send=? WHERE id=?", (later.isoformat(timespec="seconds"), e["id"]))
            continue
        sender, full = pick_sender(con, json.loads(e["c_numbers"] or "[]"), e["phone"], day, int(st["perNumberDaily"] or 100))
        if full == "all":
            break  # every number hit its daily limit
        if not sender:
            continue  # this person's number is at its limit today; tomorrow
        body = render(steps[e["step"]]["text"], e, cfg)
        try:
            cl = cl or _client(client)
            m = cl.messages.create(to=e["phone"], from_=sender, body=body)
        except Exception as ex:
            code = str(getattr(ex, "code", "") or "")
            if code == "21610":
                dnc_add(con, cfg, e["phone"], "stop", "Twilio: recipient had replied STOP")
            con.execute("UPDATE enrollments SET status='failed', reason=? WHERE id=?",
                        ((_ERR.get(code) or f"Twilio: {str(ex)[:140]}"), e["id"]))
            log(f"  campaign send failed {e['phone']}: {ex}")
            continue
        con.execute("INSERT OR IGNORE INTO messages(phone,direction,body,sid,at,campaign_id,sender,status,step) VALUES(?,?,?,?,?,?,?,?,?)",
                    (e["phone"], "out", body, getattr(m, "sid", None), naive.isoformat(timespec="seconds"), e["campaign_id"], sender,
                     getattr(m, "status", None) or "queued", e["step"]))
        remember_sender(con, e["phone"], sender)
        nxt_step = e["step"] + 1
        if nxt_step < len(steps):
            base = datetime.fromisoformat(e["enrolled_at"] or naive.isoformat())
            nxt = max(base + timedelta(days=int(steps[nxt_step]["day"])), naive + timedelta(hours=GAP_HOURS))
            con.execute("UPDATE enrollments SET step=?, last_sent=?, next_send=?, sender=? WHERE id=?",
                        (nxt_step, naive.isoformat(timespec="seconds"), nxt.isoformat(timespec="seconds"), sender, e["id"]))
        else:
            con.execute("UPDATE enrollments SET step=?, last_sent=?, status='done', sender=? WHERE id=?",
                        (nxt_step, naive.isoformat(timespec="seconds"), sender, e["id"]))
        con.commit()
        sent += 1
        if pace:
            time.sleep(pace)
    con.commit()
    _finish_campaigns(con)
    if sent:
        log(f"Campaigns: sent {sent} text(s).")
    return sent


def _finish_campaigns(con):
    for c in con.execute("SELECT id FROM campaigns WHERE status='running'").fetchall():
        if not con.execute("SELECT 1 FROM enrollments WHERE campaign_id=? AND status IN ('active','pending') LIMIT 1", (c["id"],)).fetchone():
            if con.execute("SELECT 1 FROM enrollments WHERE campaign_id=? LIMIT 1", (c["id"],)).fetchone():
                con.execute("UPDATE campaigns SET status='done', finished_at=?, updated=? WHERE id=?", (now(), now(), c["id"]))
    con.commit()


def on_inbound(con, phone, to_number=None, stop=False):
    """Any reply stops this person's drips. STOP marks them stopped everywhere."""
    if to_number:
        remember_sender(con, phone, sms.e164(to_number))
    con.execute("UPDATE enrollments SET status=?, reason=? WHERE phone=? AND status IN ('active','pending')",
                ("stopped" if stop else "replied", "Replied STOP" if stop else "Replied", phone))
    if stop:
        con.execute("INSERT INTO dnc_meta(phone,source,reason,added_at) VALUES(?,?,?,?) ON CONFLICT(phone) DO UPDATE SET source='stop', reason=excluded.reason",
                    (phone, "stop", "Replied STOP", now()))
    con.commit()


def refresh_status(con, cfg, client=None, limit=80):
    """Delivery receipts: re-reads recent outbound messages that aren't final yet."""
    since = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    rows = con.execute("""SELECT id, sid, phone FROM messages WHERE direction='out' AND sid IS NOT NULL AND at>=?
                          AND (status IS NULL OR status NOT IN ('delivered','undelivered','failed','read','canceled')) ORDER BY id DESC LIMIT ?""",
                       (since, limit)).fetchall()
    if not rows:
        return 0
    cl, n = _client(client), 0
    for r in rows:
        try:
            m = cl.messages(r["sid"]).fetch()
        except Exception:
            continue
        code = str(getattr(m, "error_code", "") or "")
        con.execute("UPDATE messages SET status=?, error_code=? WHERE id=?", (getattr(m, "status", None), code or None, r["id"]))
        if code == "21610":
            dnc_add(con, cfg, r["phone"], "stop", "Twilio: recipient had replied STOP")
        n += 1
    con.commit()
    return n


def legacy_migrate(con, cfg):
    """Moves the old config.yaml follow-up sequence into a drip campaign so it shows (and is controlled) in JARVIS."""
    if db.kv_get(con, "out_migrated"):
        return 0
    seq = (cfg.get("sms") or {}).get("sequence") or []
    rows = con.execute("SELECT * FROM contacts WHERE status='active'").fetchall()
    n = 0
    if rows and seq:
        steps = [{"day": int(s.get("day", 0)), "text": s.get("text", "")} for s in seq]
        con.execute("INSERT OR IGNORE INTO campaigns(id,name,kind,status,numbers,steps,created,updated,launched_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ("cmp_followup", "Follow-up sequence", "drip", "running", "[]", _j(steps), now(), now(), now(),
                     "Moved here from config.yaml"))
        for c in rows:
            n += con.execute("""INSERT OR IGNORE INTO enrollments(campaign_id,phone,name,first,address,source,consent,step,next_send,status,enrolled_at,last_sent)
                                VALUES('cmp_followup',?,?,?,?,'sms enroll',?,?,?,'active',?,?)""",
                             (c["phone"], c["full_name"] or "", c["first_name"] or "", c["address"] or "", c["consent"] or 0, c["step"] or 0,
                              c["next_send"] or now(), c["enrolled_at"] or now(), c["last_sent"])).rowcount
            con.execute("UPDATE contacts SET status='idle' WHERE id=?", (c["id"],))
    db.kv_set(con, "out_migrated", now())
    con.commit()
    return n


# ---------------------------------------------------------------- numbers
def owned_numbers(con, client=None, refresh=False, cached_only=False):
    raw = db.kv_get(con, "out_numbers")
    cache = json.loads(raw) if raw else None
    if cached_only or (cache and not refresh and time.time() - cache.get("t", 0) < 600):
        return (cache or {}).get("numbers") or ([{"phone": sms.e164(os.environ.get("TWILIO_FROM")), "name": "From .env", "sms": True, "voice": True}]
                                                if sms.e164(os.environ.get("TWILIO_FROM")) else [])
    cl = _client(client)
    nums = [{"phone": n.phone_number, "sid": n.sid, "name": n.friendly_name or "",
             "sms": bool((n.capabilities or {}).get("sms")), "voice": bool((n.capabilities or {}).get("voice"))}
            for n in cl.incoming_phone_numbers.list(limit=100)]
    db.kv_set(con, "out_numbers", _j({"t": time.time(), "numbers": nums}))
    return nums


def numbers(con, cfg, client=None, refresh=False):
    ok, why = twilio_ready()
    try:
        nums = owned_numbers(con, client, refresh=refresh) if ok else owned_numbers(con, cached_only=True)
        err = "" if ok else why
    except Exception as e:
        nums, err = owned_numbers(con, cached_only=True), f"Couldn't read your Twilio numbers: {str(e)[:160]}"
    since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    day = datetime.now().strftime("%Y-%m-%d")
    st = settings(con)
    out = []
    for n in nums:
        p = n["phone"]
        r = con.execute("""SELECT COUNT(*) sent, SUM(status IN ('delivered','read')) dl, SUM(status IN ('failed','undelivered')) fl,
                           SUM(error_code='30007') filt FROM messages WHERE direction='out' AND sender=? AND at>=?""", (p, since)).fetchone()
        replies = con.execute("""SELECT COUNT(DISTINCT m.phone) FROM messages m JOIN sender_map s ON s.phone=m.phone
                                 WHERE m.direction='in' AND s.sender=? AND m.at>=?""", (p, since)).fetchone()[0]
        stops = con.execute("SELECT COUNT(*) FROM dnc_meta d JOIN sender_map s ON s.phone=d.phone WHERE d.source='stop' AND s.sender=? AND d.added_at>=?",
                            (p, since)).fetchone()[0]
        sent = r["sent"] or 0
        fail_rate = round(100 * (r["fl"] or 0) / sent, 1) if sent else 0.0
        health = "Good" if fail_rate < 3 and not (r["filt"] or 0) else ("Watch" if fail_rate < 10 else "Poor")
        out.append(dict(n, sent_7d=sent, delivered_7d=r["dl"] or 0, failed_7d=r["fl"] or 0, filtered_7d=r["filt"] or 0,
                        replies_7d=replies, optouts_7d=stops, fail_rate=fail_rate, health=health if sent else "No sends yet",
                        sent_today=_sent_today(con, day, p), default=(p == st["defaultNumber"])))
    return {"numbers": out, "error": err, "settings": st, "daily_limit": int(cfg["sms"].get("daily_limit", 150)),
            "quiet_hours": cfg["sms"].get("quiet_hours", {"start": "09:00", "end": "20:00"}),
            "texting_enabled": bool(cfg["sms"].get("enabled")), "require_consent": bool(cfg["sms"].get("require_consent", True)),
            "messaging_service": bool(os.environ.get("TWILIO_MESSAGING_SERVICE_SID"))}


def search_numbers(con, a, client=None):
    area = re.sub(r"\D", "", str(a.get("area_code") or ""))
    if len(area) != 3:
        raise ValueError("Enter a 3-digit area code.")
    cl = _client(client)
    res = cl.available_phone_numbers("US").local.list(area_code=int(area), sms_enabled=True, voice_enabled=True, limit=10)
    return [{"phone": r.phone_number, "name": r.friendly_name, "city": r.locality or "", "state": r.region or ""} for r in res]


def buy_number(con, a, client=None):
    p = sms.e164(a.get("phone"))
    if not p:
        raise ValueError("Pick a number first.")
    cl = _client(client)
    n = cl.incoming_phone_numbers.create(phone_number=p)
    note = ""
    ms = os.environ.get("TWILIO_MESSAGING_SERVICE_SID")
    if ms:
        try:
            cl.messaging.v1.services(ms).phone_numbers.create(phone_number_sid=n.sid)
            note = "Added to your A2P messaging service."
        except Exception as e:
            note = f"Bought, but couldn't add it to your messaging service: {str(e)[:120]}"
    else:
        note = "Add it to your A2P campaign's messaging service in Twilio before texting from it."
    owned_numbers(con, cl, refresh=True)
    return {"phone": n.phone_number, "sid": n.sid, "note": note}


def set_settings(con, a):
    cur = settings(con)
    patch = {k: a[k] for k in DEFAULTS if k in a}
    for k in ("myCell", "defaultNumber"):
        if k in patch and patch[k]:
            if not sms.e164(patch[k]):
                raise ValueError("That isn't a valid US phone number.")
            patch[k] = sms.e164(patch[k])
    for k in ("perNumberDaily", "batch"):
        if k in patch:
            patch[k] = max(1, min(int(patch[k]), 500 if k == "perNumberDaily" else 100))
    for k in ("callStart", "callEnd"):
        if k in patch and not re.fullmatch(r"\d\d:\d\d", str(patch[k])):
            raise ValueError("Use HH:MM for calling hours.")
    if patch.get("callStart", cur["callStart"]) < "08:00" or patch.get("callEnd", cur["callEnd"]) > "21:00":
        raise ValueError("Calling hours have to stay between 8:00 AM and 9:00 PM.")
    cur.update(patch)
    db.kv_set(con, "out_settings", _j(cur))
    return cur


# ---------------------------------------------------------------- calls
def place_call(con, cfg, a, client=None, now_dt=None):
    st = settings(con)
    my = sms.e164(st["myCell"])
    if not my:
        raise ValueError("Add your cell number first (Numbers → Dialer settings). JARVIS rings you, then connects the seller.")
    p = sms.e164(a.get("phone"))
    if not p:
        raise ValueError("That isn't a valid US phone number.")
    if p in dnc_set(con, cfg):
        raise ValueError("This number is on Do Not Contact.")
    blocked, _ = sms._crm_blocked(con)
    if p in blocked:
        raise ValueError("This deal is opted out or archived.")
    hf = _hunter_flags(con, p)
    if hf["suppressed"]:
        raise ValueError("This number is suppressed in The Hunter.")
    if hf["hunter"] and not hf["manual"] and not a.get("deal_id"):
        raise ValueError("Mark this number \"OK to call manually\" in The Hunter first (after checking DNC).")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    hm = (now_dt or datetime.now(tz)).strftime("%H:%M")
    if not (st["callStart"] <= hm < st["callEnd"]):
        raise ValueError(f"Outside calling hours ({st['callStart']}–{st['callEnd']}).")
    sticky = con.execute("SELECT sender FROM sender_map WHERE phone=?", (p,)).fetchone()
    sender = sms.e164(a.get("from")) or (sticky and sticky["sender"]) or st["defaultNumber"]
    if not sender:
        raise ValueError("No calling number: pick a default number in Numbers.")
    twiml = (f'<Response><Say voice="alice">Connecting your JARVIS call.</Say>'
             f'<Dial callerId="{sender}" timeout="30" answerOnBridge="true"><Number>{p}</Number></Dial></Response>')
    call = _client(client).calls.create(to=my, from_=sender, twiml=twiml)
    con.execute("INSERT OR REPLACE INTO dialer_calls(sid,to_n,from_n,my_cell,deal_id,name,at) VALUES(?,?,?,?,?,?,?)",
                (call.sid, p, sender, my, a.get("deal_id"), str(a.get("name") or "")[:120], now()))
    remember_sender(con, p, sender)
    con.commit()
    return {"sid": call.sid, "to": p, "from": sender, "ringing": my[-4:]}


def call_log(con, cfg, client=None, days=30):
    ok, why = twilio_ready()
    names = _names(con)
    dial = {r["sid"]: dict(r) for r in con.execute("SELECT * FROM dialer_calls")}
    if not ok:
        return {"calls": [], "error": why}
    try:
        calls = _client(client).calls.list(start_time_after=datetime.utcnow() - timedelta(days=days), limit=300)
    except Exception as e:
        return {"calls": [], "error": f"Couldn't read calls from Twilio: {str(e)[:160]}"}
    rows, children = [], {}
    for c in calls:
        if getattr(c, "parent_call_sid", None):
            children.setdefault(c.parent_call_sid, []).append(c)

    def t(c):
        v = getattr(c, "start_time", None) or getattr(c, "date_created", None)
        return v.isoformat() if hasattr(v, "isoformat") else str(v or "")
    for c in calls:
        dirn = getattr(c, "direction", "")
        if c.sid in dial:
            kid = (children.get(c.sid) or [None])[0]
            d = dial[c.sid]
            status = kid.status if kid else ("you didn't pick up" if c.status in ("no-answer", "busy", "canceled") else c.status)
            rows.append({"sid": c.sid, "type": "dialer", "contact": d["to_n"], "name": d.get("name") or names.get(d["to_n"], ""),
                         "number": d["from_n"], "status": status, "duration": int((kid.duration if kid else 0) or 0), "at": t(c),
                         "missed": False, "deal_id": d.get("deal_id")})
        elif getattr(c, "parent_call_sid", None) in dial:
            continue
        elif dirn == "inbound":
            frm = sms.e164(c.from_) or c.from_
            rows.append({"sid": c.sid, "type": "inbound", "contact": frm, "name": names.get(frm, ""), "number": c.to, "status": c.status,
                         "duration": int(c.duration or 0), "at": t(c), "missed": c.status in ("no-answer", "busy", "canceled", "failed") or int(c.duration or 0) == 0})
        elif dirn.startswith("outbound") and not getattr(c, "parent_call_sid", None):
            to = sms.e164(c.to) or c.to
            rows.append({"sid": c.sid, "type": "outbound", "contact": to, "name": names.get(to, ""), "number": c.from_, "status": c.status,
                         "duration": int(c.duration or 0), "at": t(c), "missed": False})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"calls": rows, "error": ""}


# ---------------------------------------------------------------- usage & dashboard
def usage(con, client=None):
    ok, why = twilio_ready()
    if not ok:
        return {"error": why}
    cl = _client(client)
    out = {"error": "", "items": []}
    try:
        b = cl.balance.fetch()
        out["balance"], out["currency"] = float(b.balance), b.currency
    except Exception as e:
        out["balance_error"] = str(e)[:160]
    for cat, label in [("sms-outbound", "Texts sent"), ("sms-inbound", "Texts received"), ("calls", "Calls"),
                       ("phonenumbers", "Phone numbers"), ("totalprice", "Total this month")]:
        try:
            for r in cl.usage.records.this_month.list(category=cat, limit=1):
                out["items"].append({"category": cat, "label": label, "count": r.count, "usage": r.usage, "unit": r.usage_unit,
                                     "price": float(r.price or 0), "currency": r.price_unit})
        except Exception:
            pass
    return out


def overview(con, cfg):
    day = datetime.now().strftime("%Y-%m-%d")
    week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    hours = {h: {"sent": 0, "replies": 0} for h in range(24)}
    for r in con.execute("SELECT direction, substr(at,12,2) h, COUNT(*) n FROM messages WHERE at LIKE ? GROUP BY direction, h", (day + "%",)):
        try:
            hours[int(r["h"])]["sent" if r["direction"] == "out" else "replies"] += r["n"]
        except (TypeError, ValueError):
            pass
    sent_w = con.execute("SELECT COUNT(DISTINCT phone) FROM messages WHERE direction='out' AND at>=?", (week,)).fetchone()[0]
    rep_w = con.execute("""SELECT COUNT(DISTINCT i.phone) FROM messages i WHERE i.direction='in' AND i.at>=?
                           AND EXISTS(SELECT 1 FROM messages o WHERE o.phone=i.phone AND o.direction='out')""", (week,)).fetchone()[0]
    temp = {"hot": 0, "warm": 0, "cold": 0}
    try:
        for r in con.execute("""SELECT c.lead_status s, COUNT(*) n FROM contacts c WHERE EXISTS(SELECT 1 FROM messages m WHERE m.phone=c.phone
                                AND m.direction='in' AND m.at>=?) GROUP BY c.lead_status""", (week,)):
            if r["s"] in temp:
                temp[r["s"]] = r["n"]
    except Exception:
        pass
    repliers_w = con.execute("SELECT COUNT(DISTINCT phone) FROM messages WHERE direction='in' AND at>=?", (week,)).fetchone()[0]
    running = [{"id": c["id"], "name": c["name"], "kind": c["kind"], "status": c["status"], **_stats(con, c["id"])}
               for c in con.execute("SELECT * FROM campaigns WHERE status IN ('running','paused') ORDER BY updated DESC")]
    end = datetime.now().replace(hour=23, minute=59).isoformat(timespec="seconds")
    queued = con.execute("""SELECT COUNT(*) FROM enrollments e JOIN campaigns c ON c.id=e.campaign_id
                            WHERE c.status='running' AND e.status='active' AND e.next_send<=?""", (end,)).fetchone()[0]
    nums = owned_numbers(con, cached_only=True)
    return {"sent_today": _sent_today(con, day), "replies_today": sum(h["replies"] for h in hours.values()),
            "response_rate_7d": round(100 * rep_w / sent_w, 1) if sent_w else 0.0, "contacted_7d": sent_w, "repliers_7d": repliers_w,
            "temperature": temp, "hours": [hours[h] for h in range(24)], "campaigns": running, "queued_today": queued,
            "numbers": len(nums), "gate": send_gate(con, cfg), "daily_limit": int(cfg["sms"].get("daily_limit", 150)),
            "failed_today": con.execute("SELECT COUNT(*) FROM messages WHERE direction='out' AND at LIKE ? AND status IN ('failed','undelivered')",
                                        (day + "%",)).fetchone()[0]}


# ---------------------------------------------------------------- app entry point
READ_OPS = {"overview", "campaigns", "campaign", "preview", "numbers", "calls", "dnc", "usage", "search_numbers", "settings"}
WRITE_OPS = {"save_campaign", "add_audience", "remove_audience", "launch", "pause", "resume", "cancel", "duplicate",
             "dnc_add", "dnc_remove", "call", "buy_number", "set_settings", "refresh_numbers"}


def handle(con, cfg, req):
    a = req or {}
    op = a.get("op")
    if op == "overview":
        return overview(con, cfg)
    if op == "campaigns":
        return {"campaigns": list_campaigns(con, cfg), "gate": send_gate(con, cfg)}
    if op == "campaign":
        return get_campaign(con, cfg, a.get("id"))
    if op == "preview":
        return preview(con, cfg, a.get("id"))
    if op == "numbers":
        return numbers(con, cfg)
    if op == "refresh_numbers":
        return numbers(con, cfg, refresh=True)
    if op == "calls":
        return call_log(con, cfg, days=int(a.get("days") or 30))
    if op == "dnc":
        return {"items": dnc_list(con, cfg)}
    if op == "usage":
        return usage(con)
    if op == "search_numbers":
        return {"results": search_numbers(con, a)}
    if op == "settings":
        return settings(con)
    if op == "save_campaign":
        return save_campaign(con, cfg, a)
    if op == "add_audience":
        return add_audience(con, cfg, a)
    if op == "remove_audience":
        return remove_audience(con, cfg, a)
    if op == "launch":
        return launch(con, cfg, a.get("id"))
    if op in ("pause", "resume", "cancel"):
        return set_status(con, a.get("id"), {"pause": "paused", "resume": "running", "cancel": "cancelled"}[op])
    if op == "duplicate":
        return duplicate(con, a.get("id"))
    if op == "dnc_add":
        return dnc_add(con, cfg, a.get("phone"), "manual", str(a.get("reason") or ""))
    if op == "dnc_remove":
        return dnc_remove(con, cfg, a.get("phone"))
    if op == "call":
        return place_call(con, cfg, a)
    if op == "buy_number":
        return buy_number(con, a)
    if op == "set_settings":
        return set_settings(con, a)
    raise ValueError(f"Unknown outreach operation: {op}")
