"""LinkedIn owner list: about 20 business owners a day for the owner to connect with by hand.

Nothing here touches LinkedIn. The engine picks owners from the AI Sidekicks CRM (HVAC, plumbing, roofing and real estate),
drafts a short connection note for each, and gives a LinkedIn search link. The owner opens the link, pastes the note,
and sends it himself, then marks it Sent. When someone accepts, a first message is drafted the same way.
Automating LinkedIn itself (bots opening profiles or typing into message boxes) gets accounts restricted, so it isn't done.
"""
import json
import re
from datetime import datetime
from urllib.parse import quote

from . import db

DAILY = 20
NOTE_MAX = 280  # LinkedIn allows 300 characters on a connection note (Premium); keep a margin
MSG_MAX = 600
AREA = "Massachusetts"

# niche id -> (label, how to recognise it from the CRM record, LinkedIn searches for new owners)
NICHES = {
    "hvac": ("HVAC", r"\bhvac\b|heating|cooling|air ?condition|furnace|boiler|mechanical|heat pump",
             ["owner HVAC", "president heating and cooling", "owner HVAC company"]),
    "plumbing": ("Plumbing", r"plumb|drain|sewer|septic|water heater",
                 ["owner plumbing", "master plumber owner", "president plumbing company"]),
    "roofing": ("Roofing", r"roof|gutter|siding",
                ["owner roofing", "president roofing company", "owner roofing contractor"]),
    "real_estate": ("Real estate", r"property manag|real estate|realty|realtor|brokerage|\bproperties\b|rentals?\b|leasing",
                    ["owner property management", "broker owner real estate", "owner real estate brokerage"]),
}
ORDER = ["hvac", "plumbing", "roofing", "real_estate"]
STATUSES = ("queued", "sent", "accepted", "messaged", "replied", "skipped")
NEXT_OK = {  # which marks are allowed from each status
    "queued": {"sent", "skipped"},
    "sent": {"accepted", "replied", "skipped", "queued"},
    "accepted": {"messaged", "replied", "skipped"},
    "messaged": {"replied", "skipped"},
    "replied": {"skipped"},
    "skipped": {"queued"},
}
PROFILE_RE = re.compile(r"^https://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]{2,100}/?$")
IN_CONVERSATION = ("Replied", "Interested", "Discovery Scheduled", "Discovery Completed", "Solution Design", "Proposal Sent",
                   "Negotiation", "Won", "Lost", "Disqualified")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_li_outreach(business_id TEXT PRIMARY KEY, company TEXT, owner TEXT, owner_title TEXT, niche TEXT,
  city TEXT, state TEXT, search_url TEXT, profile_url TEXT, note TEXT, note_by TEXT, followup TEXT, followup_by TEXT,
  status TEXT DEFAULT 'queued', day TEXT, sent_at TEXT, accepted_at TEXT, messaged_at TEXT, replied_at TEXT, source TEXT,
  created TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS hq_li_day ON hq_li_outreach(day);
CREATE INDEX IF NOT EXISTS hq_li_status ON hq_li_outreach(status);
"""


def ensure(con):
    con.executescript(SCHEMA)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def niche_of(rec):
    """Which of the four niches a CRM record belongs to, or None."""
    ind = str(rec.get("industry") or "").lower()
    text = " ".join(str(rec.get(k) or "") for k in ("name", "services", "industry", "notes")).lower()
    if "property management" in ind or "real estate" in ind:
        return "real_estate"
    if re.search(NICHES["plumbing"][1], text):
        return "plumbing"
    for n in ("hvac", "roofing", "real_estate"):
        if re.search(NICHES[n][1], text):
            return n
    return None


def search_link(keywords):
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(re.sub(r"\s+", " ", keywords).strip())


def niche_searches():
    """Free LinkedIn people searches for finding new owners in each niche."""
    return [{"niche": n, "label": NICHES[n][0], "searches": [{"q": f"{q} {AREA}", "url": search_link(f"{q} {AREA}")} for q in NICHES[n][2]]}
            for n in ORDER]


def first_name(full):
    w = re.findall(r"[A-Za-z][A-Za-z'\-]+", str(full or ""))
    w = [x for x in w if x.lower() not in ("mr", "mrs", "ms", "dr", "the")]
    return w[0].capitalize() if w else ""


def _owner(dc, bid):
    rows = dc.execute("SELECT name, title, role FROM dc_contacts WHERE business_id=? AND COALESCE(name,'')<>'' AND COALESCE(suppressed,0)=0 "
                      "ORDER BY CASE WHEN role='decision_maker' THEN 0 ELSE 1 END, CASE WHEN COALESCE(title,'')<>'' THEN 0 ELSE 1 END", (bid,)).fetchall()
    for r in rows:
        n = re.sub(r"\s+", " ", r["name"] or "").strip()
        if 3 <= len(n) <= 60 and "@" not in n and len(n.split()) <= 4:
            return n, re.sub(r"\s*\((?:SDR research)\)\s*", "", r["title"] or "").strip()[:80]
    return "", ""


def _facts(dc, bid, limit=4):
    return [f"{r['field']}: {r['value']}"[:200] for r in dc.execute(
        "SELECT field, value FROM dc_facts WHERE business_id=? AND field IN ('research','services','tools','online_booking','software','size') "
        "ORDER BY id LIMIT ?", (bid, limit))]


def candidates(con, dc, limit):
    """CRM businesses in the four niches, not already on the LinkedIn list, not already in a conversation; owner known first."""
    ensure(con)
    done = {r[0] for r in con.execute("SELECT business_id FROM hq_li_outreach")}
    rows = dc.execute(f"SELECT * FROM dc_businesses WHERE stage NOT IN ({','.join('?' * len(IN_CONVERSATION))}) "
                      "AND COALESCE(disqualify_reason,'')='' ORDER BY score DESC", IN_CONVERSATION).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        if b["id"] in done:
            continue
        n = niche_of(b)
        if not n:
            continue
        owner, title = _owner(dc, b["id"])
        out.append({"business_id": b["id"], "company": b["name"], "owner": owner, "owner_title": title, "niche": n,
                    "city": b.get("city") or "", "state": b.get("state") or "", "services": (b.get("services") or "")[:200],
                    "stage": b.get("stage"), "score": b.get("score") or 0, "facts": _facts(dc, b["id"])})
    out.sort(key=lambda c: (0 if c["owner"] else 1, -int(c["score"] or 0)))
    return out[:limit] if limit else out


def template_note(c):
    first = first_name(c.get("owner"))
    who = {"hvac": "HVAC", "plumbing": "plumbing", "roofing": "roofing", "real_estate": "property management and real estate"}[c["niche"]]
    where = c.get("city") or AREA
    return (f"Hi {first}, " if first else "Hi, ") + (
        f"I'm Mola with AI Sidekicks. We build AI assistants for {who} businesses around {where} that pick up missed calls "
        "and handle the follow-up admin. Would be good to connect.")[:NOTE_MAX]


def template_followup(c):
    first = first_name(c.get("owner"))
    q = {"hvac": "when a call comes in after hours or while the techs are out on jobs, who picks it up?",
         "plumbing": "when an emergency call comes in after hours or while you're on a job, who picks it up?",
         "roofing": "when a homeowner calls for a quote while the crew's up on a roof, who gets back to them, and how fast?",
         "real_estate": "how are tenant calls and maintenance requests handled after hours right now?"}[c["niche"]]
    return (f"Thanks for connecting{', ' + first if first else ''}. Quick question about {c['company']}: {q}")[:MSG_MAX]


def _check(text, limit):
    """Problems with a drafted note: too long, a link, or a claim we can't back up."""
    from . import agents_ai
    t = str(text or "").strip()
    bad = []
    if not t:
        bad.append("empty")
    if len(t) > limit:
        bad.append(f"{len(t)} characters (limit {limit})")
    if re.search(r"https?://|www\.|\.com\b", t, re.I):
        bad.append("contains a link")
    bad += agents_ai.claims_found(t)
    return bad


SYSTEM = ("You write short, plain LinkedIn messages for Mola, founder of AI Sidekicks, a small AI agency that builds "
          "assistants for small businesses (answering missed calls, booking, follow-ups, admin). The reader is a busy business owner. "
          "Sound like a person, not a marketer: no hype, no superlatives, no numbers or results we haven't proven, no links, no emojis, "
          "no pitch for a call in a connection note.")


def _draft(con, kind, people, limit, how, ask=None):
    """AI drafts for a batch of people; returns {key: text}. Anything that fails the checks falls back to the template."""
    from . import agents_ai, hq
    ask = ask or agents_ai.ask
    data = {"offer": hq.di_offer(con)[:800], "people": [
        {"key": p["business_id"], "first_name": first_name(p.get("owner")), "company": p["company"], "niche": NICHES[p["niche"]][0],
         "city": p.get("city") or AREA, "services": p.get("services", ""), "facts": p.get("facts", [])[:4]} for p in people]}
    try:
        out, _meta = ask(con, agent="sdr", kind=kind, task_id=None, business="demigod", tier="fast", system=SYSTEM,
                         instructions=how, data=data, output_spec='"drafts": [{"key": "<the person\'s key>", "text": "<the message>"}]',
                         max_tokens=min(6000, 220 * len(people) + 400))
    except Exception:  # noqa: BLE001  cap reached, no key, network: the templates still give him a usable list
        return {}
    got = {}
    for d in (out or {}).get("drafts") or []:
        if isinstance(d, dict) and d.get("key") and not _check(d.get("text"), limit):
            got[str(d["key"])] = re.sub(r"\s+", " ", str(d["text"])).strip()
    return got


NOTE_HOW = (f"Write one LinkedIn connection note per person, at most {NOTE_MAX} characters each. Start with 'Hi <first_name>,' "
            "(or just 'Hi,' when first_name is empty). Say in one sentence what Mola does at AI Sidekicks, tied to their trade and town, using one "
            "true detail from their record when there is one. End with a low-key line about connecting. No question that asks for a call.")
FOLLOWUP_HOW = (f"They accepted Mola's connection request. Write the first message, at most {MSG_MAX} characters. Thank them for "
                "connecting, then ask ONE short, specific question about how they handle calls, scheduling or follow-ups today, "
                "based on their trade and record. Don't pitch, don't offer a demo or a call yet, don't sign off with a name.")


def build(con, limit=DAILY, day=None, ask=None, use_ai=True):
    """Fill today's list up to `limit`, counting unsent ones carried over from earlier days. Only drafts; never sends."""
    from . import dcrm
    ensure(con)
    day = day or _today()
    limit = max(1, min(int(limit or DAILY), 40))
    have = con.execute("SELECT COUNT(*) FROM hq_li_outreach WHERE day=? OR (status='queued' AND day<?)", (day, day)).fetchone()[0]
    need = limit - have
    if need <= 0:
        return {"added": 0, "day": day, "note": "Today's list is already full."}
    dc = dcrm.connect()
    picks = candidates(con, dc, need)
    if not picks:
        return {"added": 0, "day": day, "note": "No owners left in the CRM for these niches. Use the LinkedIn searches to find new ones and add them."}
    drafts = _draft(con, "sdr.linkedin_notes", picks, NOTE_MAX, NOTE_HOW, ask) if use_ai else {}
    t = db.now()
    for p in picks:
        note = drafts.get(p["business_id"])
        con.execute("""INSERT OR IGNORE INTO hq_li_outreach(business_id,company,owner,owner_title,niche,city,state,search_url,note,note_by,
                       status,day,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["business_id"], p["company"], p["owner"], p["owner_title"], p["niche"], p["city"], p["state"],
                     search_link(f"{p['owner']} {p['company']}" if p["owner"] else p["company"]),
                     note or template_note(p), "ai" if note else "template", "queued", day, "crm", t, t))
    con.commit()
    return {"added": len(picks), "day": day, "ai_drafted": len(drafts), "left_in_crm": max(0, len(candidates(con, dc, 0)))}


def _row(r):
    d = dict(r)
    d["niche_label"] = NICHES.get(d.get("niche"), ("",))[0]
    d["link"] = d.get("profile_url") or d.get("search_url")
    return d


def today(con, day=None):
    """Today's list (plus unsent carry-overs and accepted ones waiting for a first message), totals and the niche searches."""
    from . import dcrm
    ensure(con)
    day = day or _today()
    rows = con.execute("""SELECT * FROM hq_li_outreach WHERE day=? OR (status='queued' AND day<?) OR status='accepted'
                          ORDER BY CASE status WHEN 'accepted' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, day, created""", (day, day)).fetchall()
    tot = {s: 0 for s in STATUSES}
    for s, n in con.execute("SELECT status, COUNT(*) FROM hq_li_outreach GROUP BY status"):
        tot[s] = n
    sent_ever = sum(tot[s] for s in ("sent", "accepted", "messaged", "replied"))
    try:
        left = len(candidates(con, dcrm.connect(), 0))
    except Exception:  # noqa: BLE001
        left = None
    waiting = con.execute("""SELECT * FROM hq_li_outreach WHERE status IN ('sent','messaged') AND day<?
                             ORDER BY COALESCE(messaged_at, sent_at) DESC LIMIT 60""", (day,)).fetchall()
    return {"day": day, "daily": DAILY, "items": [_row(r) for r in rows], "waiting": [_row(r) for r in waiting], "totals": tot, "sent_ever": sent_ever,
            "accept_rate": round((tot["accepted"] + tot["messaged"] + tot["replied"]) / sent_ever, 2) if sent_ever else None,
            "left_in_crm": left, "searches": niche_searches(), "note_limit": NOTE_MAX,
            "sent_today": con.execute("SELECT COUNT(*) FROM hq_li_outreach WHERE substr(COALESCE(sent_at,''),1,10)=?", (day,)).fetchone()[0]}


def mark(con, business_id, status, profile_url=None, note=None, followup=None, reply=None, ask=None, use_ai=True):
    """The owner marks what he did on LinkedIn. Accepted -> a first message is drafted. Replied -> the CRM stage moves to Replied."""
    from . import dcrm
    ensure(con)
    r = con.execute("SELECT * FROM hq_li_outreach WHERE business_id=?", (business_id,)).fetchone()
    if not r:
        raise ValueError("That owner isn't on the LinkedIn list.")
    cur = dict(r)
    sets = {}
    if profile_url is not None:
        u = str(profile_url).strip().split("?")[0]
        if u and not PROFILE_RE.match(u):
            raise ValueError("Paste the profile link as it appears in the address bar, like https://www.linkedin.com/in/their-name/")
        sets["profile_url"] = u
    if note is not None:
        n = re.sub(r"\s+", " ", str(note)).strip()
        if len(n) > NOTE_MAX + 20:
            raise ValueError(f"A connection note can be at most 300 characters (this one is {len(n)}).")
        sets["note"], sets["note_by"] = n, "owner"
    if followup is not None:
        sets["followup"], sets["followup_by"] = str(followup).strip()[:2000], "owner"
    t = db.now()
    dc = None
    if status and status != cur["status"]:
        if status not in STATUSES:
            raise ValueError(f"Unknown status. Use one of: {', '.join(STATUSES)}")
        if status not in NEXT_OK[cur["status"]]:
            raise ValueError(f"Can't go from {cur['status']} to {status}.")
        sets["status"] = status
        stamp = {"sent": "sent_at", "accepted": "accepted_at", "messaged": "messaged_at", "replied": "replied_at"}.get(status)
        if stamp:
            sets[stamp] = t
        if status == "queued":
            sets["day"] = _today()
        dc = dcrm.connect()
        fact = {"sent": "connection request sent", "accepted": "accepted the connection request", "messaged": "first message sent",
                "replied": "replied"}.get(status)
        if fact:
            try:
                dcrm.add_fact(dc, business_id, "linkedin", f"LinkedIn: {fact} ({t[:10]})", "verified", sets.get("profile_url") or cur.get("profile_url") or "", by="owner")
                dc.commit()
            except Exception:  # noqa: BLE001  the business may have been merged away; the list still updates
                pass
        if status == "accepted" and not cur.get("followup"):
            c = dict(cur, facts=_facts(dc, business_id))
            d = _draft(con, "sdr.linkedin_followup", [dict(c, services="")], MSG_MAX, FOLLOWUP_HOW, ask) if use_ai else {}
            sets["followup"] = d.get(business_id) or template_followup(c)
            sets["followup_by"] = "ai" if d.get(business_id) else "template"
        if status == "replied":
            try:
                dcrm.move(dc, business_id, "Replied", by="owner", note="Replied on LinkedIn" + (f": {str(reply).strip()[:300]}" if reply else ""))
            except Exception:  # noqa: BLE001
                pass
    if not sets:
        return {"item": _row(cur), "changed": False}
    sets["updated"] = t
    con.execute(f"UPDATE hq_li_outreach SET {', '.join(k + '=?' for k in sets)} WHERE business_id=?", (*sets.values(), business_id))
    con.commit()
    return {"item": _row(con.execute("SELECT * FROM hq_li_outreach WHERE business_id=?", (business_id,)).fetchone()), "changed": True}


def add(con, company, owner="", title="", profile_url="", niche="", city="", state="MA", ask=None, use_ai=True):
    """An owner the owner found himself on LinkedIn: adds the business to the CRM and puts them on today's list with a drafted note."""
    from . import dcrm
    ensure(con)
    company = re.sub(r"\s+", " ", str(company or "")).strip()
    owner = re.sub(r"\s+", " ", str(owner or "")).strip()
    if not company:
        raise ValueError("Add the company name.")
    if niche not in NICHES:
        raise ValueError(f"Pick a niche: {', '.join(NICHES[n][0] for n in ORDER)}")
    u = str(profile_url or "").strip().split("?")[0]
    if u and not PROFILE_RE.match(u):
        raise ValueError("Paste the profile link as it appears in the address bar, like https://www.linkedin.com/in/their-name/")
    dc = dcrm.connect()
    ind = "property management" if niche == "real_estate" else "home services"
    bid, created, _how = dcrm.upsert(dc, {"name": company, "city": city, "state": state, "industry": ind, "services": NICHES[niche][0],
                                          "contact_name": owner, "contact_title": title or "Owner", "source": u}, source="linkedin")
    dc.commit()
    if con.execute("SELECT 1 FROM hq_li_outreach WHERE business_id=?", (bid,)).fetchone():
        if u:
            con.execute("UPDATE hq_li_outreach SET profile_url=?, updated=? WHERE business_id=?", (u, db.now(), bid)); con.commit()
        return {"business_id": bid, "created": False, "already_listed": True, "item": _row(con.execute("SELECT * FROM hq_li_outreach WHERE business_id=?", (bid,)).fetchone())}
    p = {"business_id": bid, "company": company, "owner": owner, "owner_title": title or "Owner", "niche": niche, "city": city, "state": state,
         "services": "", "facts": _facts(dc, bid)}
    d = _draft(con, "sdr.linkedin_notes", [p], NOTE_MAX, NOTE_HOW, ask) if use_ai else {}
    t = db.now()
    con.execute("""INSERT INTO hq_li_outreach(business_id,company,owner,owner_title,niche,city,state,search_url,profile_url,note,note_by,
                   status,day,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, company, owner, p["owner_title"], niche, city, state, search_link(f"{owner} {company}".strip()), u,
                 d.get(bid) or template_note(p), "ai" if d.get(bid) else "template", "queued", _today(), "linkedin", t, t))
    con.commit()
    return {"business_id": bid, "created": created, "item": _row(con.execute("SELECT * FROM hq_li_outreach WHERE business_id=?", (bid,)).fetchone())}
