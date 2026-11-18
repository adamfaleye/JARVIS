"""AI Sidekicks CRM (milestone 1): one record per business, many contacts, every field with its source,
an explainable score, and a 17-stage pipeline where a move needs evidence.

Source of truth is this database; the JARVIS app reads and edits it through the PC link (hq.di_* ops), so the
SDR's discoveries, the emails sent from Gmail and the replies read every 10 minutes all land on the same record.

Rules
- Dedupe: website domain, business email, phone (last 10 digits), or name + city. Same business = same record,
  whatever contact details a new source brings; the new details are added as facts and contacts.
- Provenance: every fact carries a grade (verified, public, third_party, inferred, invalid) and a source URL.
  Inferred emails are kept for reference but never used for outreach.
- Score (0-100) with a reason per part and an evidence strength (strong / moderate / weak / unknown). Industry alone
  is worth little; evidence of inquiries, scheduling or admin load is what scores.
- Stages: agents and the system can only move a business forward with evidence the system itself recorded (a sent
  email, a logged reply, a booked meeting, a returned DocuSign envelope). You can move one by hand; your note is
  stored as the evidence. Every move is timestamped in the stage history.
"""
import json
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from . import db

STAGES = ["Newly Discovered", "Researching", "Qualified", "Ready for Outreach", "Awaiting Approval", "Contacted", "Follow-Up Due",
          "Replied", "Interested", "Discovery Scheduled", "Discovery Completed", "Solution Design", "Proposal Sent", "Negotiation",
          "Won", "Lost", "Disqualified"]
CLOSED = ("Won", "Lost", "Disqualified")
ORDER = {s: i for i, s in enumerate(STAGES)}
APP_STAGE = {"Lead": "Qualified", "Discovery": "Discovery Scheduled", "Proposal": "Proposal Sent", "Contracted": "Won", "Lost": "Lost"}
GRADES = ("verified", "public", "third_party", "inferred", "invalid")
EMAIL_CLASS = ("verified", "public_unverified", "third_party", "inferred", "invalid", "none")
FREEMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "comcast.net", "verizon.net", "msn.com", "live.com",
            "me.com", "att.net", "protonmail.com", "ymail.com"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS dc_businesses(id TEXT PRIMARY KEY, name TEXT, name_key TEXT, domain TEXT, phone_key TEXT, email_key TEXT,
  industry TEXT, city TEXT, state TEXT, address TEXT, website TEXT, phone TEXT, email TEXT, contact_page TEXT, size TEXT, services TEXT,
  ops TEXT, opportunities TEXT, stage TEXT, stage_at TEXT, score INTEGER, priority TEXT, score_detail TEXT, assigned TEXT DEFAULT 'sdr',
  next_action TEXT, next_date TEXT, disqualify_reason TEXT, lost_reason TEXT, notes TEXT, discovered_at TEXT, researched_at TEXT,
  source TEXT, app_id TEXT, created TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS dc_b_domain ON dc_businesses(domain);
CREATE INDEX IF NOT EXISTS dc_b_phone ON dc_businesses(phone_key);
CREATE INDEX IF NOT EXISTS dc_b_name ON dc_businesses(name_key);
CREATE TABLE IF NOT EXISTS dc_contacts(id TEXT PRIMARY KEY, business_id TEXT, name TEXT, title TEXT, role TEXT, email TEXT, phone TEXT,
  email_class TEXT, source_url TEXT, found_at TEXT, verified_at TEXT, verify_note TEXT, suppressed INTEGER DEFAULT 0, UNIQUE(business_id, email));
CREATE TABLE IF NOT EXISTS dc_facts(id INTEGER PRIMARY KEY, business_id TEXT, field TEXT, value TEXT, grade TEXT, source_url TEXT, found_at TEXT, by TEXT,
  UNIQUE(business_id, field, value));
CREATE TABLE IF NOT EXISTS dc_stage_history(id INTEGER PRIMARY KEY, business_id TEXT, from_stage TEXT, to_stage TEXT, at TEXT, by TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS dc_links(business_id TEXT, prospect_id TEXT, PRIMARY KEY(business_id, prospect_id));
CREATE TABLE IF NOT EXISTS dc_ingested(ref TEXT PRIMARY KEY, at TEXT);
"""


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    con.commit()
    return con


def now():
    return db.now()


# ------------------------------------------------------------------ keys
def name_key(n):
    s = re.sub(r"[^a-z0-9 ]", " ", str(n or "").lower())
    s = re.sub(r"\b(llc|inc|co|corp|corporation|company|ltd|the|and|services|service|group)\b", " ", s)
    return re.sub(r"\s+", "", s)[:60]


# Listing and directory sites: a page there is about the business, not its own website, so it never identifies it.
DIRECTORIES = {"yelp.com", "facebook.com", "instagram.com", "linkedin.com", "google.com", "bbb.org", "yellowpages.com", "angi.com", "homeadvisor.com",
               "thumbtack.com", "houzz.com", "nextdoor.com", "mapquest.com", "manta.com", "managemyproperty.com", "porch.com", "birdeye.com",
               "superpages.com", "chamberofcommerce.com", "zillow.com", "apartments.com", "allpropertymanagement.com", "x.com", "twitter.com"}


def _directory(d):
    return any(d == x or d.endswith("." + x) for x in DIRECTORIES)


def domain_of(url_or_email):
    s = str(url_or_email or "").strip().lower()
    if not s:
        return ""
    if "@" in s and "/" not in s:
        d = s.split("@", 1)[1]
        return "" if d in FREEMAIL else d
    if not re.match(r"^https?://", s):
        s = "http://" + s
    d = urllib.parse.urlparse(s).netloc.split(":")[0]
    d = d[4:] if d.startswith("www.") else d
    return d if "." in d and d not in FREEMAIL and not _directory(d) else ""


def phone_key(p):
    d = re.sub(r"\D", "", str(p or ""))
    return d[-10:] if len(d) >= 10 else ""


def email_ok(e):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", str(e or "").strip().lower()))


# ------------------------------------------------------------------ free email checks (no paid provider)
def _mx(domain, fetch=None):
    """Does the domain accept mail? DNS-over-HTTPS (Google), no extra install. True / False / None (couldn't check)."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": domain, "type": "MX"})
    try:
        raw = fetch(url) if fetch else urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/dns-json"}), timeout=10).read().decode()
        j = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if j.get("Status") == 3:  # NXDOMAIN
        return False
    return bool([a for a in (j.get("Answer") or []) if a.get("type") == 15])


def check_email(email, source_url="", fetch=None, site_domain=""):
    """Classify an address. 'verified' is reserved for a paid verifier; these checks can only find invalid ones.
    site_domain: the business's own website, so a free-mail address it publishes there (shop@verizon.net) counts as published."""
    e = str(email or "").strip().lower()
    if not email_ok(e):
        return "invalid", "not a valid address"
    dom = e.split("@", 1)[1]
    mx = _mx(dom, fetch)
    if mx is False:
        return "invalid", f"{dom} doesn't accept email (no MX record)"
    role = e.split("@", 1)[0] in ("info", "office", "contact", "hello", "sales", "admin", "service", "support", "team", "inquiries", "frontdesk")
    note = ("role inbox" if role else "named inbox") + ("; domain accepts mail" if mx else "; mail check unavailable")
    if not str(source_url).startswith("http"):
        return "third_party", note + "; no source page"
    host = urllib.parse.urlparse(source_url).netloc.lower()
    if dom in FREEMAIL and site_domain and _same_site(host, site_domain):
        return "public_unverified", note + "; free-mail address published on their own site " + host
    if not _same_site(host, dom):
        return "third_party", note + "; listed on " + host + " (not their own site)"
    return "public_unverified", note + "; published on " + host


def _same_site(host, email_domain):
    h = host[4:] if host.startswith("www.") else host
    return h == email_domain or h.endswith("." + email_domain) or email_domain.endswith("." + h)


# ------------------------------------------------------------------ records
def find_match(con, rec):
    dom = domain_of(rec.get("website")) or domain_of(rec.get("email"))
    pk, ek = phone_key(rec.get("phone")), str(rec.get("email") or "").strip().lower()
    nk, city = name_key(rec.get("name") or rec.get("company")), str(rec.get("city") or "").strip().lower()
    checks = [("domain", dom), ("phone_key", pk), ("email_key", ek)]
    for col, v in checks:
        if v:
            r = con.execute(f"SELECT * FROM dc_businesses WHERE {col}=? LIMIT 1", (v,)).fetchone()
            if r:
                return dict(r), col
    if ek:
        r = con.execute("SELECT b.* FROM dc_contacts c JOIN dc_businesses b ON b.id=c.business_id WHERE lower(c.email)=? LIMIT 1", (ek,)).fetchone()
        if r:
            return dict(r), "contact email"
    if nk and city:
        r = con.execute("SELECT * FROM dc_businesses WHERE name_key=? AND lower(city)=? LIMIT 1", (nk, city)).fetchone()
        if r:
            return dict(r), "name + city"
    if nk and len(nk) >= 12:  # a distinctive name where one record has no city is the same business
        r = con.execute("SELECT * FROM dc_businesses WHERE name_key=? AND (?='' OR COALESCE(city,'')='') LIMIT 1", (nk, city)).fetchone()
        if r:
            return dict(r), "name (one record had no city)"
    return None, ""


def add_fact(con, bid, field, value, grade, source_url="", by="sdr"):
    v = str(value or "").strip()
    if not v:
        return
    con.execute("INSERT OR IGNORE INTO dc_facts(business_id,field,value,grade,source_url,found_at,by) VALUES(?,?,?,?,?,?,?)",
                (bid, field, v[:500], grade if grade in GRADES else "third_party", str(source_url or "")[:400], now(), by))


def add_contact(con, bid, email="", name="", title="", role="", phone="", source_url="", email_class=None, fetch=None, check=True):
    e = str(email or "").strip().lower()
    if e and con.execute("SELECT 1 FROM dc_contacts WHERE business_id=? AND email=?", (bid, e)).fetchone():
        return None
    if e:
        site = (con.execute("SELECT domain FROM dc_businesses WHERE id=?", (bid,)).fetchone() or [""])[0] or ""
        cls, note = (email_class, "") if email_class else (check_email(e, source_url, fetch, site) if check else ("public_unverified" if source_url else "third_party", ""))
    else:
        cls, note = "none", ""
    cid = "dct_" + uuid.uuid4().hex[:10]
    con.execute("""INSERT INTO dc_contacts(id,business_id,name,title,role,email,phone,email_class,source_url,found_at,verify_note)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, bid, str(name or "")[:120], str(title or "")[:120], role or ("general" if e and e.split("@")[0] in ("info", "office", "contact", "hello") else "contact"),
                 e or None, str(phone or "")[:40], cls, str(source_url or "")[:400], now(), note[:200]))
    return cid


def upsert(con, rec, source="sdr", fetch=None, check_email_dns=True):
    """Add a business or merge into the one it already is. rec: name/company, website, email, email_source, phone, city, state,
    address, industry, size, services, notes, source (URL), contact_name, contact_title. Returns (id, created, matched_on)."""
    name = str(rec.get("name") or rec.get("company") or "").strip()
    if not name:
        raise ValueError("A business needs a name.")
    src_url = str(rec.get("source") or rec.get("source_url") or "")
    cur, how = find_match(con, rec)
    t = now()
    if cur:
        bid = cur["id"]
        fill = {}
        for k in ("website", "phone", "city", "state", "address", "industry", "size", "services", "contact_page"):
            if rec.get(k) and not cur.get(k):
                fill[k] = str(rec[k])[:300]
        dom = domain_of(rec.get("website")) or domain_of(rec.get("email"))
        if dom and not cur.get("domain"):
            fill["domain"] = dom
        if phone_key(rec.get("phone")) and not cur.get("phone_key"):
            fill["phone_key"] = phone_key(rec.get("phone"))
        if fill:
            con.execute(f"UPDATE dc_businesses SET {', '.join(k + '=?' for k in fill)}, updated=? WHERE id=?", (*fill.values(), t, bid))
        created = False
    else:
        bid = "dcb_" + uuid.uuid4().hex[:10]
        con.execute("""INSERT INTO dc_businesses(id,name,name_key,domain,phone_key,email_key,industry,city,state,address,website,phone,size,services,
                       stage,stage_at,discovered_at,source,notes,created,updated,assigned) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid, name[:160], name_key(name), domain_of(rec.get("website")) or domain_of(rec.get("email")), phone_key(rec.get("phone")),
                     str(rec.get("email") or "").strip().lower() if email_ok(rec.get("email")) else "", str(rec.get("industry") or "").strip().lower()[:80], str(rec.get("city") or "")[:80], str(rec.get("state") or "")[:20],
                     str(rec.get("address") or "")[:200], str(rec.get("website") or "")[:200], str(rec.get("phone") or "")[:40],
                     str(rec.get("size") or "")[:80], str(rec.get("services") or "")[:300], "Newly Discovered", t, t, source,
                     str(rec.get("notes") or "")[:500], t, t, "sdr"))
        con.execute("INSERT INTO dc_stage_history(business_id,from_stage,to_stage,at,by,evidence) VALUES(?,?,?,?,?,?)",
                    (bid, None, "Newly Discovered", t, source, json.dumps({"source": src_url or source})))
        created = True
    g = "public" if src_url.startswith("http") else "third_party"
    for k in ("website", "phone", "city", "industry", "size", "services", "address"):
        add_fact(con, bid, k, rec.get(k), g, src_url, source)
    if rec.get("notes"):
        add_fact(con, bid, "note", rec["notes"], g, src_url, source)
    if rec.get("email"):
        add_contact(con, bid, rec["email"], rec.get("contact_name", ""), rec.get("contact_title", ""), "", rec.get("phone", ""),
                    rec.get("email_source") or src_url, fetch=fetch, check=check_email_dns)
    elif rec.get("contact_name"):
        add_contact(con, bid, "", rec["contact_name"], rec.get("contact_title", ""), "decision_maker", "", src_url)
    if rec.get("prospect_id"):
        con.execute("INSERT OR IGNORE INTO dc_links(business_id,prospect_id) VALUES(?,?)", (bid, rec["prospect_id"]))
    con.commit()
    score(con, bid)
    return bid, created, how


# ------------------------------------------------------------------ scoring
ACTIVE_INDUSTRIES = ("home services", "hvac", "plumbing", "roofing", "electrical", "landscaping", "general contracting", "property management",
                     "auto repair", "cleaning", "restoration", "accounting", "law", "insurance", "consulting", "staffing", "restaurant",
                     "hospitality", "salon", "fitness", "retail")
INQUIRY = re.compile(r"book(ing)?|appointment|schedul|free (quote|estimate)|request (a |an )?(quote|estimate|service)|24\s*/\s*7|emergency|service request|maintenance request|reservation", re.I)
ADMIN = re.compile(r"locations|branches|crew|technicians|team of|employees|portal|financing|membership|invoice|permit|work order|property owners|tenants|clients", re.I)


NEGATION = re.compile(r"\b(no|not|never|without|none|nothing|lacks?|lacking|unable|cannot|can'?t|couldn'?t|could not|didn'?t|did not|"
                      r"doesn'?t|does not|don'?t|do not|isn'?t|aren'?t|wasn'?t|weren'?t)\b", re.I)
CLAUSE_BREAK = re.compile(r"[.;!?\n]|\b(?:but|however|although|though|while|whereas)\b", re.I)


def hits(rx, text):
    """Matches of rx that the same clause doesn't negate: "no booking software found" is not evidence that they take bookings."""
    text = str(text or "")
    out = []
    for m in rx.finditer(text):
        before = text[max(0, m.start() - 80):m.start()]
        cuts = [x.end() for x in CLAUSE_BREAK.finditer(before)]
        if cuts:
            before = before[cuts[-1]:]
        if not NEGATION.search(before):
            out.append(m)
    return out


def _facts(con, bid):
    return [dict(r) for r in con.execute("SELECT field, value, grade, source_url FROM dc_facts WHERE business_id=?", (bid,))]


def score(con, bid):
    b = dict(con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone())
    facts = _facts(con, bid)
    text = " ".join(f["value"] for f in facts) + " " + " ".join(str(b.get(k) or "") for k in ("services", "ops", "notes", "size"))
    contacts = [dict(r) for r in con.execute("SELECT * FROM dc_contacts WHERE business_id=? AND suppressed=0", (bid,))]
    parts, unknown = {}, 0

    def part(key, pts, mx, why, strength):
        parts[key] = {"points": pts, "max": mx, "why": why, "evidence": strength}

    ind = (b.get("industry") or "").lower()
    if ind and any(a in ind for a in ACTIVE_INDUSTRIES):
        part("industry", 8, 10, f"{ind} is an industry AI Sidekicks is testing (fit alone doesn't prove a need)", "moderate")
    elif ind:
        part("industry", 4, 10, f"{ind}: not one of the industries being tested yet", "weak")
    else:
        part("industry", 0, 10, "industry unknown", "unknown"); unknown += 1
    sizes = hits(re.compile(r"(\d{1,4})\s*(?:\+|-\s*\d+)?\s*(employees|staff|technicians|people)", re.I), text)
    m = sizes[0] if sizes else None
    if m:
        n = int(m.group(1))
        ok = 2 <= n <= 250
        part("size", 15 if ok else 4, 15, f"about {n} {m.group(2)} ({'in' if ok else 'outside'} the 2-250 range)", "strong" if ok else "weak")
    elif b.get("size"):
        part("size", 9, 15, f"size noted: {b['size']}", "moderate")
    else:
        part("size", 5, 15, "size unknown", "unknown"); unknown += 1
    inq = sorted({x.group(0).lower() for x in hits(INQUIRY, text)})
    if len(inq) >= 2:
        part("inquiries", 20, 20, "takes customer inquiries/scheduling: " + ", ".join(inq[:4]), "strong")
    elif inq:
        part("inquiries", 12, 20, "some sign of inquiries/scheduling: " + inq[0], "moderate")
    else:
        part("inquiries", 0, 20, "no evidence yet of how they take inquiries or bookings", "unknown"); unknown += 1
    adm = sorted({x.group(0).lower() for x in hits(ADMIN, text)})
    if len(adm) >= 2:
        part("admin", 15, 15, "signs of admin/coordination load: " + ", ".join(adm[:4]), "strong")
    elif adm:
        part("admin", 8, 15, "some sign of admin load: " + adm[0], "moderate")
    else:
        part("admin", 0, 15, "no evidence yet of repetitive admin work", "unknown"); unknown += 1
    usable = [c for c in contacts if c["email_class"] in ("verified", "public_unverified", "third_party")]
    best = "verified" if any(c["email_class"] == "verified" for c in usable) else "public_unverified" if any(c["email_class"] == "public_unverified" for c in usable) else "third_party" if usable else ""
    if best:
        part("contact", {"verified": 20, "public_unverified": 15, "third_party": 8}[best], 20,
             {"verified": "verified business email", "public_unverified": "business email published on their own site (not deliverability-verified)",
              "third_party": "email from a third-party listing"}[best], "strong" if best != "third_party" else "moderate")
    elif b.get("phone") or b.get("contact_page"):
        part("contact", 6, 20, "phone or contact form only", "weak")
    else:
        part("contact", 0, 20, "no way to reach them yet", "unknown"); unknown += 1
    dm = [c for c in contacts if c.get("name") and c.get("title")]
    part("decision_maker", 10 if dm else 0, 10, f"decision maker on record: {dm[0]['name']}, {dm[0]['title']}" if dm else "decision maker not found yet",
         "strong" if dm else "unknown")
    srcs = len({f["source_url"] for f in facts if f["source_url"]})
    part("quality", min(10, srcs * 4), 10, f"{srcs} source page(s) behind the record", "strong" if srcs >= 2 else "weak" if srcs else "unknown")
    total = sum(p["points"] for p in parts.values())
    if b.get("disqualify_reason") or b.get("stage") == "Disqualified":
        pri = "Disqualified"
    elif unknown >= 3:
        pri = "Research required"
    else:
        pri = "High" if total >= 65 else "Medium" if total >= 45 else "Low"
    auto = {"Research their website and services", "Find a published email", "Draft a first email", ""}
    nxt = b.get("next_action") or ""
    if nxt in auto and ORDER.get(b.get("stage"), 0) > ORDER["Qualified"]:
        nxt = ""  # past qualification the next step comes from the pipeline (follow-ups, replies), not from the score
    elif nxt in auto:  # a next action you or the app set is never overwritten
        nxt = ("Research their website and services" if pri == "Research required" else "Find a published email" if parts["contact"]["points"] < 8
               else "Draft a first email" if pri in ("High", "Medium") else "")
    con.execute("UPDATE dc_businesses SET score=?, priority=?, score_detail=?, next_action=?, updated=? WHERE id=?",
                (total, pri, json.dumps({"parts": parts, "unknown": unknown, "at": now()}), nxt if b.get("stage") not in CLOSED else "", now(), bid))
    con.commit()
    return total, pri, parts


# ------------------------------------------------------------------ pipeline
def _evidence(con, b, to):
    """What the system itself recorded that justifies moving to `to`. Returns a dict or None."""
    pids = [r[0] for r in con.execute("SELECT prospect_id FROM dc_links WHERE business_id=?", (b["id"],))]
    emails = [r[0] for r in con.execute("SELECT email FROM dc_contacts WHERE business_id=? AND email IS NOT NULL", (b["id"],))]

    def tasks(kind, status="completed"):
        out = []
        for r in con.execute("SELECT id, params, result, finished FROM hq_tasks WHERE kind=? AND status=?", (kind, status)):
            p = json.loads(r["params"] or "{}")
            if p.get("prospect_id") in pids or str(p.get("to") or p.get("attendee") or p.get("signer_email") or "").lower() in emails:
                out.append(dict(r))
        return out
    if to == "Researching":
        return {"by_system": "record created"}
    if to == "Qualified":
        sd = json.loads(b.get("score_detail") or "{}")
        if (b.get("priority") in ("High", "Medium")) and (sd.get("parts") or {}).get("contact", {}).get("points", 0) > 0:
            return {"score": b.get("score"), "priority": b.get("priority")}
        return None
    if to == "Ready for Outreach":
        if b.get("stage") != "Qualified" and _evidence(con, b, "Qualified") is None:
            return None  # only qualified businesses are made ready
        c = con.execute("SELECT email, email_class FROM dc_contacts WHERE business_id=? AND suppressed=0 AND email_class IN ('verified','public_unverified','third_party') LIMIT 1", (b["id"],)).fetchone()
        return {"contact": c["email"], "class": c["email_class"]} if c else None
    if to == "Awaiting Approval":
        t = tasks("sdr.send_email", "awaiting_approval")
        if t:
            return {"email_task": t[0]["id"]}
        r = con.execute("SELECT ci.id FROM hq_campaign_items ci WHERE ci.prospect_id IN (%s) AND ci.status IN ('ready','approved')" % ",".join("?" * len(pids)), pids).fetchone() if pids else None
        return {"campaign_item": r[0]} if r else None
    if to == "Contacted":
        t = tasks("sdr.send_email")
        return {"email_task": t[0]["id"], "sent": t[0]["finished"]} if t else None
    if to == "Follow-Up Due":
        from . import hq
        due = {d["prospect_id"] for d in hq.followups_due(con)["due"]}
        hit = [p for p in pids if p in due]
        return {"follow_up_due": hit[0]} if hit else None
    if to in ("Replied", "Interested"):
        if not pids:
            return None
        rows = con.execute(f"SELECT id, class, at, text FROM hq_email_replies WHERE prospect_id IN ({','.join('?' * len(pids))}) ORDER BY at DESC", pids).fetchall()
        real = [r for r in rows if not re.search(r"ticket number|has been received|auto(matic)?[- ]?reply|out of (the )?office", r["text"] or "", re.I)]
        if to == "Replied" and real:
            return {"reply": real[0]["id"], "at": real[0]["at"]}
        hot = [r for r in real if r["class"] in ("interested", "meeting")]
        return {"reply": hot[0]["id"], "class": hot[0]["class"], "at": hot[0]["at"]} if (to == "Interested" and hot) else None
    if to == "Discovery Scheduled":
        t = tasks("ae.book_meeting")
        return {"meeting_task": t[0]["id"], "event": (json.loads(t[0]["result"] or "{}") or {}).get("event_id")} if t else None
    if to == "Solution Design":
        for k in ("architect.crm_plan", "architect.requirements"):
            t = tasks(k)
            if t:
                return {"plan_task": t[0]["id"]}
        return None
    if to == "Proposal Sent":
        t = tasks("ae.send_agreement")
        return {"agreement_task": t[0]["id"]} if t else None
    return None  # Discovery Completed, Negotiation, Won need your word (or a signed-envelope check in a later milestone)


def move(con, bid, to, by="owner", note="", reason=""):
    """Move a business to a stage. The owner may move it with a note (stored as the evidence); anyone else needs system evidence."""
    if to not in STAGES:
        raise ValueError(f"Unknown stage. Stages: {', '.join(STAGES)}")
    r = con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone()
    if not r:
        raise ValueError("That business isn't in the CRM.")
    b = dict(r)
    if b["stage"] == to:
        return {"status": "unchanged", "stage": to}
    if to in ("Lost", "Disqualified") and not (reason or note).strip():
        raise ValueError(f"Moving to {to} needs a reason.")
    ev = _evidence(con, b, to)
    if ev is None:
        if by != "owner":
            raise ValueError(f"No evidence on record for '{to}' (e.g. a sent email, a logged reply, a booked meeting); the stage stays {b['stage']}.")
        if to not in ("Lost", "Disqualified") and not note.strip():
            raise ValueError(f"Say what happened (a short note) to move it to {to}; it's saved as the evidence.")
        ev = {"owner_note": note.strip()[:500]}
    elif note:
        ev["note"] = note.strip()[:500]
    t = now()
    sets = {"stage": to, "stage_at": t, "updated": t}
    if to == "Disqualified":
        sets["disqualify_reason"] = (reason or note)[:300]
    if to == "Lost":
        sets["lost_reason"] = (reason or note)[:300]
    if to == "Researching" and not b.get("researched_at"):
        sets["researched_at"] = t
    con.execute(f"UPDATE dc_businesses SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), bid))
    con.execute("INSERT INTO dc_stage_history(business_id,from_stage,to_stage,at,by,evidence) VALUES(?,?,?,?,?,?)",
                (bid, b["stage"], to, t, by, json.dumps(ev)))
    con.commit()
    score(con, bid)
    return {"status": "ok", "from": b["stage"], "stage": to, "evidence": ev}


def advance(con, bid):
    """Move a business as far forward as recorded evidence allows (system moves only; never backwards, never to a closed stage)."""
    moved = []
    for _ in range(len(STAGES)):
        b = dict(con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone())
        if b["stage"] in CLOSED:
            break
        nxt = None
        for cand in reversed(STAGES[ORDER[b["stage"]] + 1:STAGES.index("Won")]):
            if cand in ("Discovery Completed", "Negotiation"):
                continue
            if _evidence(con, b, cand) is not None:
                nxt = cand
                break
        if not nxt:
            break
        move(con, bid, nxt, by="system")
        moved.append(nxt)
    return moved


# ------------------------------------------------------------------ ingest what JARVIS already has
def ingest_searches(con, fetch=None, check_email_dns=True):
    """Businesses from the SDR's discovery searches (each result once)."""
    from . import hq
    n_new = n_merged = 0
    for s in hq.searches(con, 50)["searches"]:
        if not s["done"]:
            continue
        r = con.execute("SELECT * FROM hq_prospect_searches WHERE id=?", (s["id"],)).fetchone()
        for p in hq._search_found(con, dict(r)):
            ref = f"search:{s['id']}:{name_key(p.get('company'))}:{str(p.get('email') or '').lower()}"
            if con.execute("SELECT 1 FROM dc_ingested WHERE ref=?", (ref,)).fetchone():
                continue
            rec = dict(p, industry=s.get("industry") or "property management", source=p.get("source") or "")
            bid, created, _ = upsert(con, rec, source="sdr discovery", fetch=fetch, check_email_dns=check_email_dns)
            con.execute("INSERT OR IGNORE INTO dc_ingested(ref,at) VALUES(?,?)", (ref, now()))
            con.commit()
            n_new += created
            n_merged += not created
    return {"new": n_new, "merged": n_merged}


def ingest_campaigns(con, fetch=None, check_email_dns=True):
    """Prospects already emailed through campaigns (linked by prospect id, so sends and replies attach)."""
    n = 0
    for r in con.execute("SELECT prospect_id, company, to_addr, info FROM hq_campaign_items WHERE info IS NOT NULL").fetchall():
        ref = "campaign:" + r["prospect_id"]
        if con.execute("SELECT 1 FROM dc_ingested WHERE ref=?", (ref,)).fetchone():
            continue
        info = json.loads(r["info"] or "{}")
        rec = {**info, "company": r["company"] or info.get("company"), "email": r["to_addr"] or info.get("email"),
               "email_source": info.get("source"), "prospect_id": r["prospect_id"], "industry": info.get("industry") or "property management"}
        upsert(con, rec, source="campaign", fetch=fetch, check_email_dns=check_email_dns)
        con.execute("INSERT OR IGNORE INTO dc_ingested(ref,at) VALUES(?,?)", (ref, now()))
        con.commit()
        n += 1
    return {"campaign_prospects": n}


def ingest_app(con, items, fetch=None):
    """The JARVIS app's existing AI Sidekicks prospects (company, contact, email, phone, industry, source, pain, value, stage, next action)."""
    n = 0
    for p in items or []:
        if not p.get("company"):
            continue
        ref = "app:" + str(p.get("id"))
        rec = {"company": p["company"], "email": p.get("email"), "phone": p.get("phone"), "industry": p.get("industry"), "notes": p.get("pain"),
               "contact_name": p.get("contact"), "source": p.get("source") if str(p.get("source") or "").startswith("http") else ""}
        bid, created, _ = upsert(con, rec, source="app", fetch=fetch)
        con.execute("UPDATE dc_businesses SET app_id=COALESCE(app_id, ?), next_action=COALESCE(NULLIF(?, ''), next_action), next_date=COALESCE(NULLIF(?, ''), next_date) WHERE id=?",
                    (str(p.get("id")), p.get("nextAction") or "", p.get("nextDate") or "", bid))
        want = APP_STAGE.get(p.get("stage") or "")
        if want and ORDER[want] > ORDER[dict(con.execute("SELECT stage FROM dc_businesses WHERE id=?", (bid,)).fetchone())["stage"]]:
            try:
                move(con, bid, want, by="owner", note=f"Set to {p.get('stage')} in the JARVIS app before the CRM upgrade", reason="imported from the app")
            except ValueError:
                pass
        con.execute("INSERT OR IGNORE INTO dc_ingested(ref,at) VALUES(?,?)", (ref, now()))
        con.commit()
        n += 1
    return {"app_prospects": n}


def enrich(con, limit=10, fetch=None, log=print):
    """Read the websites of businesses not yet researched (free: no AI, no paid data) and record what the pages say, with
    the page as the source: what they do, booking/estimate/24-7 language, portals, staff or location counts, software, chat."""
    from . import precall
    rows = con.execute("""SELECT id, website, domain FROM dc_businesses WHERE researched_at IS NULL AND stage NOT IN ('Won','Lost','Disqualified')
                          AND (COALESCE(website,'')!='' OR COALESCE(domain,'')!='') ORDER BY created LIMIT ?""", (int(limit),)).fetchall()
    done = 0
    for r in rows:
        site = r["website"] or r["domain"]
        w = precall.read_website(site, fetch=fetch) if fetch else precall.read_website(site)
        bid, t = r["id"], now()
        if w.get("status") == "ok":
            url = w.get("url") or site
            if w.get("description"):
                add_fact(con, bid, "about", w["description"], "public", url, "website")
            body = w.get("text_sample") or ""
            inq = sorted({x.group(0).lower() for x in hits(INQUIRY, body)})
            if inq:
                add_fact(con, bid, "inquiries", "site mentions: " + ", ".join(inq[:6]), "public", url, "website")
            adm = sorted({x.group(0).lower() for x in hits(ADMIN, body)})
            if adm:
                add_fact(con, bid, "operations", "site mentions: " + ", ".join(adm[:6]), "public", url, "website")
            for u in w.get("unit_claims") or []:
                add_fact(con, bid, "size_claim", u, "public", url, "website")
            m = re.search(r"(\d{1,4})\s*(?:\+\s*)?(employees|staff members|technicians|team members|locations|offices)", body, re.I)
            if m:
                add_fact(con, bid, "size", m.group(0), "public", url, "website")
            if w.get("software") or w.get("tools"):
                add_fact(con, bid, "software", ", ".join((w.get("software") or []) + (w.get("tools") or [])), "public", url, "website")
            if w.get("online_booking"):
                add_fact(con, bid, "inquiries", "site offers online booking or service requests", "public", url, "website")
            if w.get("chat_widget"):
                add_fact(con, bid, "chat", ", ".join(w["chat_widget"]), "public", url, "website")
            if w.get("says_24_7"):
                add_fact(con, bid, "inquiries", "advertises 24/7 or after-hours service", "public", url, "website")
            if w.get("maintenance_form"):
                add_fact(con, bid, "inquiries", "online request form", "public", url, "website")
            con.execute("UPDATE dc_businesses SET researched_at=?, updated=? WHERE id=?", (t, t, bid))
        else:
            add_fact(con, bid, "research_note", "website: " + str(w.get("status")), "invalid" if "couldn't" in str(w.get("status")) else "public", site, "website")
            con.execute("UPDATE dc_businesses SET researched_at=?, updated=? WHERE id=?", (t, t, bid))
        con.commit()
        score(con, bid)
        done += 1
    return done


def apply_sdr_result(con, bid, kind, out, fetch=None, check_email_dns=True):
    """Save what an SDR lookup found onto the business record, each piece with the page it came from, then rescore.
    kind: sdr.find_contact (email, website, contact page) or sdr.research_prospect (sourced facts, decision maker, software)."""
    r = con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone()
    if not r:
        raise ValueError("That business isn't in the CRM.")
    b, out, t, saved = dict(r), dict(out or {}), now(), []
    web = str(out.get("website") or "").strip()
    fill = {}
    if web and domain_of(web) and not b.get("website"):
        fill["website"] = web[:200]
    if web and domain_of(web) and not b.get("domain"):
        fill["domain"] = domain_of(web)
    page = str(out.get("contact_page") or "").strip()
    if page.startswith("http") and not b.get("contact_page"):
        fill["contact_page"] = page[:300]
    if fill:
        con.execute(f"UPDATE dc_businesses SET {', '.join(k + '=?' for k in fill)}, updated=? WHERE id=?", (*fill.values(), t, bid))
        saved += list(fill)
    if web and domain_of(web):
        add_fact(con, bid, "website", web, "public" if web.startswith("http") else "third_party", web if web.startswith("http") else "", "sdr")
    if kind == "sdr.find_contact":
        e = str(out.get("email") or "").strip().lower()
        if email_ok(e):
            if add_contact(con, bid, e, phone=str(out.get("phone") or ""), source_url=str(out.get("email_source") or ""), fetch=fetch, check=check_email_dns):
                saved.append("email")
        else:
            add_fact(con, bid, "research_note", "SDR found no published email" + (" (contact form instead)" if page else ""), "public" if page else "third_party", page, "sdr")
    elif kind == "sdr.research_prospect":
        n = 0
        for f in out.get("facts") or []:
            fact = str((f or {}).get("fact") or "").strip() if isinstance(f, dict) else str(f or "").strip()
            src = str((f or {}).get("source") or "").strip() if isinstance(f, dict) else ""
            if fact:
                add_fact(con, bid, "research", fact, "public" if src.startswith("http") else "third_party", src if src.startswith("http") else "", "sdr")
                n += 1
        if n:
            saved.append(f"{n} fact(s)")
        sw = str(out.get("software") or "").strip()
        if sw and not NEGATION.search(sw) and "unknown" not in sw.lower():
            add_fact(con, bid, "software", sw, "third_party", "", "sdr")
        dm = re.sub(r"\s+", " ", str(out.get("decision_maker") or "")).strip()
        if dm and not NEGATION.search(dm) and not re.search(r"unknown|unclear|unconfirmed|n/a", dm, re.I):
            m = re.match(r"^(.+?)(?:\s*\(\s*(.+?)\s*\)|\s*,\s*(.+)|\s+[-–]\s+(.+))$", dm)
            name, title = (m.group(1), m.group(2) or m.group(3) or m.group(4)) if m else (dm, "decision maker")
            src = next((str(f.get("source")) for f in out.get("facts") or [] if isinstance(f, dict) and str(f.get("source") or "").startswith("http")), "")
            if not con.execute("SELECT 1 FROM dc_contacts WHERE business_id=? AND lower(name)=lower(?)", (bid, name[:120])).fetchone():
                add_contact(con, bid, "", name[:120], (title + " (SDR research)")[:120], "decision_maker", "", src)
                saved.append("decision maker")
    con.execute("UPDATE dc_businesses SET updated=? WHERE id=?", (t, bid))
    con.commit()
    total, pri, _ = score(con, bid)
    return {"business_id": bid, "saved": saved, "score": total, "priority": pri}


FILL = ("domain", "phone_key", "email_key", "industry", "city", "state", "address", "website", "phone", "email", "contact_page", "size", "services",
        "ops", "opportunities", "notes", "discovered_at", "researched_at", "app_id")


def merge(con, keep_id, dup_id, by="owner", why="duplicate"):
    """Fold a duplicate record into the one to keep: contacts, facts, links (sent emails and replies), stage history and any
    details the keeper lacks move over; the keeper keeps the furthest stage either reached; the duplicate is removed. Logged."""
    if keep_id == dup_id:
        raise ValueError("Pick two different records.")
    k = con.execute("SELECT * FROM dc_businesses WHERE id=?", (keep_id,)).fetchone()
    d = con.execute("SELECT * FROM dc_businesses WHERE id=?", (dup_id,)).fetchone()
    if not k or not d:
        raise ValueError("Both records must be in the CRM.")
    k, d, t = dict(k), dict(d), now()
    fill = {c: d[c] for c in FILL if d.get(c) and not k.get(c)}
    if ORDER.get(d["stage"], 0) > ORDER.get(k["stage"], 0) and k["stage"] not in CLOSED:
        fill.update(stage=d["stage"], stage_at=d["stage_at"])
    if fill:
        con.execute(f"UPDATE dc_businesses SET {', '.join(c + '=?' for c in fill)} WHERE id=?", (*fill.values(), keep_id))
    have = {r[0] for r in con.execute("SELECT lower(email) FROM dc_contacts WHERE business_id=? AND email IS NOT NULL", (keep_id,))}
    for c in con.execute("SELECT id, email FROM dc_contacts WHERE business_id=?", (dup_id,)).fetchall():
        if c["email"] and c["email"].lower() in have:
            con.execute("DELETE FROM dc_contacts WHERE id=?", (c["id"],))
        else:
            con.execute("UPDATE dc_contacts SET business_id=? WHERE id=?", (keep_id, c["id"]))
    con.execute("INSERT OR IGNORE INTO dc_facts(business_id,field,value,grade,source_url,found_at,by) SELECT ?,field,value,grade,source_url,found_at,by FROM dc_facts WHERE business_id=?", (keep_id, dup_id))
    con.execute("DELETE FROM dc_facts WHERE business_id=?", (dup_id,))
    con.execute("INSERT OR IGNORE INTO dc_links(business_id,prospect_id) SELECT ?,prospect_id FROM dc_links WHERE business_id=?", (keep_id, dup_id))
    con.execute("DELETE FROM dc_links WHERE business_id=?", (dup_id,))
    con.execute("UPDATE dc_stage_history SET business_id=? WHERE business_id=?", (keep_id, dup_id))
    con.execute("INSERT INTO dc_stage_history(business_id,from_stage,to_stage,at,by,evidence) VALUES(?,?,?,?,?,?)",
                (keep_id, k["stage"], fill.get("stage", k["stage"]), t, by, json.dumps({"merged": {"id": dup_id, "name": d["name"], "why": why}})))
    con.execute("DELETE FROM dc_businesses WHERE id=?", (dup_id,))
    con.execute("UPDATE dc_businesses SET updated=? WHERE id=?", (t, keep_id))
    con.commit()
    score(con, keep_id)
    return {"kept": keep_id, "merged": dup_id, "name": k["name"], "filled": sorted(fill), "stage": fill.get("stage", k["stage"])}


def _keeper(a, b):
    """The record to keep: the one further along the pipeline, then the one with more on it, then the older one."""
    key = lambda r: (ORDER.get(r["stage"], 0), sum(1 for c in FILL if r.get(c)), -(datetime.fromisoformat(r["created"][:19]).timestamp() if r.get("created") else 0))
    return (a, b) if key(a) >= key(b) else (b, a)


def duplicates(con):
    """Pairs that are almost certainly the same business: the same phone number, or the same distinctive name where one has no city."""
    rows = [dict(r) for r in con.execute("SELECT * FROM dc_businesses")]
    pairs, seen = [], set()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            how = ("same phone" if a["phone_key"] and a["phone_key"] == b["phone_key"] else
                   "same name, one has no city" if a["name_key"] and len(a["name_key"]) >= 12 and a["name_key"] == b["name_key"]
                   and (not a.get("city") or not b.get("city") or str(a["city"]).lower() == str(b["city"]).lower()) else "")
            if how and (a["id"], b["id"]) not in seen:
                seen.add((a["id"], b["id"]))
                k, d = _keeper(a, b)
                pairs.append({"keep": k["id"], "keep_name": k["name"], "duplicate": d["id"], "duplicate_name": d["name"], "why": how})
    return {"pairs": pairs}


def auto_merge(con):
    """Merge the certain duplicates (see duplicates()). Each merge is in the kept record's history."""
    out = []
    for p in duplicates(con)["pairs"]:
        if con.execute("SELECT COUNT(*) FROM dc_businesses WHERE id IN (?,?)", (p["keep"], p["duplicate"])).fetchone()[0] == 2:
            out.append(merge(con, p["keep"], p["duplicate"], by="system", why=p["why"]))
    return out


def reclass_contacts(con):
    """Emails taken from someone else's site (Facebook, Yelp, a directory) are third-party, not published by the business."""
    n = 0
    for c in con.execute("""SELECT c.id, c.email, c.source_url, c.business_id, b.domain FROM dc_contacts c LEFT JOIN dc_businesses b ON b.id=c.business_id
                            WHERE c.email_class='public_unverified'""").fetchall():
        host = urllib.parse.urlparse(c["source_url"] or "").netloc.lower()
        dom = c["email"].split("@", 1)[1] if c["email"] else ""
        if dom in FREEMAIL and c["domain"] and _same_site(host, c["domain"]):
            continue  # a free-mail address on their own site is still published by them
        if c["email"] and host and not _same_site(host, dom):
            con.execute("UPDATE dc_contacts SET email_class='third_party', verify_note=COALESCE(verify_note,'') || '; listed on ' || ? || ' (not their own site)' WHERE id=?", (host, c["id"]))
            n += 1
    for c in con.execute("""SELECT c.id, c.email, c.source_url, c.business_id, b.domain FROM dc_contacts c JOIN dc_businesses b ON b.id=c.business_id
                            WHERE c.email_class='third_party' AND c.email IS NOT NULL""").fetchall():
        host = urllib.parse.urlparse(c["source_url"] or "").netloc.lower()
        if c["email"].split("@", 1)[1] in FREEMAIL and host and c["domain"] and _same_site(host, c["domain"]):
            con.execute("UPDATE dc_contacts SET email_class='public_unverified', verify_note=? WHERE id=?",
                        (f"free-mail address published on their own site {host}", c["id"]))
            n += 1
            score(con, c["business_id"])
    con.commit()
    return n


def mark_bounces(con):
    """An email that bounced is not a way to reach them: the contact becomes invalid and the record is rescored."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hq_email_bounces'").fetchone():
        return 0
    n = 0
    for r in con.execute("""SELECT c.id, c.business_id, b2.at FROM dc_contacts c JOIN hq_email_bounces b2 ON lower(c.email)=b2.email
                            WHERE c.email_class != 'invalid'""").fetchall():
        con.execute("UPDATE dc_contacts SET email_class='invalid', verify_note=? WHERE id=?", (f"bounced {str(r['at'])[:10]}", r["id"]))
        n += 1
        score(con, r["business_id"])
    con.commit()
    return n


def sync(con, fetch=None, check_email_dns=True, web_fetch=None, do_enrich=True):
    """The SDR's CRM duty: pull in new discoveries and campaign prospects, then advance every open record on recorded evidence."""
    reclass_contacts(con)
    mark_bounces(con)
    merged = auto_merge(con)
    a = ingest_searches(con, fetch, check_email_dns)
    b = ingest_campaigns(con, fetch, check_email_dns)
    researched = enrich(con, 10, fetch=web_fetch) if do_enrich else 0
    moved = 0
    for r in con.execute("SELECT id FROM dc_businesses WHERE stage NOT IN ('Won','Lost','Disqualified')").fetchall():
        moved += len(advance(con, r["id"]))
        score(con, r["id"])
    out = {**a, **b, "researched": researched, "stage_moves": moved, "merged": len(merged)}
    db.kv_set(con, "dcrm_sync_last", json.dumps({**out, "at": now()}))
    return out


# ------------------------------------------------------------------ read models
def _row(r, full=False):
    d = dict(r)
    d["score_detail"] = json.loads(d.get("score_detail") or "{}")
    if not full:
        d["why"] = [p["why"] for p in (d["score_detail"].get("parts") or {}).values() if p.get("points")][:3]
        d.pop("score_detail", None)
    return d


def businesses(con, f=None):
    f = f or {}
    where, args = ["1=1"], []
    for k in ("stage", "priority", "industry"):
        if f.get(k):
            where.append(f"lower({k})=lower(?)"); args.append(f[k])
    if f.get("q"):
        where.append("(name LIKE ? OR domain LIKE ? OR city LIKE ?)"); args += [f"%{f['q']}%"] * 3
    if not f.get("include_closed"):
        where.append("stage NOT IN ('Lost','Disqualified')")
    lim = max(1, min(int(f.get("limit") or 100), 500))
    rows = con.execute(f"SELECT * FROM dc_businesses WHERE {' AND '.join(where)} ORDER BY CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 WHEN 'Research required' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END, score DESC LIMIT ?", (*args, lim)).fetchall()
    return {"businesses": [_row(r) for r in rows], "total": con.execute(f"SELECT COUNT(*) FROM dc_businesses WHERE {' AND '.join(where)}", args).fetchone()[0]}


def detail(con, bid):
    r = con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone()
    if not r:
        raise ValueError("That business isn't in the CRM.")
    d = _row(r, full=True)
    d["contacts"] = [dict(x) for x in con.execute("SELECT * FROM dc_contacts WHERE business_id=? ORDER BY found_at", (bid,))]
    d["facts"] = _facts(con, bid)
    d["history"] = [dict(x, evidence=json.loads(x["evidence"] or "{}")) for x in con.execute("SELECT * FROM dc_stage_history WHERE business_id=? ORDER BY id", (bid,))]
    pids = [x[0] for x in con.execute("SELECT prospect_id FROM dc_links WHERE business_id=?", (bid,))]
    from . import hq
    d["outreach"] = [e for e in hq._sent_emails(con) if e["prospect_id"] in pids]
    d["replies"] = [x for x in hq.email_replies(con, limit=500)["replies"] if x["prospect_id"] in pids]
    d["stages"] = STAGES
    return d


def summary(con):
    by = {r[0]: r[1] for r in con.execute("SELECT stage, COUNT(*) FROM dc_businesses GROUP BY stage")}
    pri = {r[0]: r[1] for r in con.execute("SELECT priority, COUNT(*) FROM dc_businesses GROUP BY priority")}
    ind = {r[0] or "unknown": r[1] for r in con.execute("SELECT industry, COUNT(*) FROM dc_businesses GROUP BY industry")}
    cls = {r[0]: r[1] for r in con.execute("SELECT email_class, COUNT(*) FROM dc_contacts GROUP BY email_class")}
    return {"total": sum(by.values()), "by_stage": {s: by.get(s, 0) for s in STAGES}, "by_priority": pri, "by_industry": ind, "emails": cls,
            "last_sync": json.loads(db.kv_get(con, "dcrm_sync_last") or "null")}
