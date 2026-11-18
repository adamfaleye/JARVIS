"""Pre-call brief for AI Sidekicks sales calls: one page on a prospect (any industry) before you talk to them, including what they use today.

What goes in (facts only, each with where it came from):
  - JARVIS's own records: how we found them (search notes, city, units), the email we sent and when, any reply.
  - Their website (fetched by the engine): what they say they do, doors/units they claim, areas served, the property
    software their tenant portal runs on (AppFolio, Buildium, Yardi...), whether they advertise 24/7 / after-hours
    maintenance, live chat, an online maintenance request form, and the phone numbers they list.
  - Up to 3 web searches by the AI for public reviews (Google / BBB / Yelp) and size, with source links.
Then Claude (fast tier, priced against the daily AI cap, about 3-5 cents) writes: who they are, likely pain points
tied to what AI Sidekicks sells, 4 questions to ask, the angle to pitch, and watch-outs. Anything it couldn't confirm is
listed as unknown, never filled in.

When: automatically for every booked AI Sidekicks meeting in the next 36 hours, and for anyone whose reply reads as
interested / wants a call. On demand: "Hey Jarvis, brief me on Mediate Management", the app, or python run.py brief <name>.
Nothing here contacts anyone.
"""
import html as _html
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from . import db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 JARVIS-Engine/1.0"
SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_precall(id INTEGER PRIMARY KEY, key TEXT UNIQUE, company TEXT, email TEXT, website TEXT, prospect_id TEXT,
  meeting_start TEXT, created TEXT, facts TEXT, brief TEXT, cost REAL, status TEXT, error TEXT);
"""
SOFTWARE = {"appfolio": "AppFolio", "buildium": "Buildium", "managebuilding.com": "Buildium", "yardi": "Yardi", "rentcafe": "Yardi (RENTCafe)",
            "rentmanager": "Rent Manager", "propertyware": "Propertyware", "rentvine": "Rentvine", "doorloop": "DoorLoop", "tenantcloud": "TenantCloud",
            "entrata": "Entrata", "realpage": "RealPage", "resman": "ResMan", "innago": "Innago", "turbotenant": "TurboTenant", "hemlane": "Hemlane",
            "rentec": "Rentec Direct", "avail.co": "Avail", "propertyboss": "PropertyBoss", "mri": "MRI"}
CHAT = {"intercom": "Intercom", "drift": "Drift", "livechatinc": "LiveChat", "tawk.to": "tawk.to", "tidio": "Tidio", "hubspot": "HubSpot chat",
        "zendesk": "Zendesk", "podium": "Podium", "birdeye": "Birdeye", "crisp.chat": "Crisp", "olark": "Olark", "elise": "EliseAI", "meetelise": "EliseAI"}
# What they use today for phones, booking and jobs (the "what they use today" part of a competitive brief), found in their pages' code.
TOOLS = {"servicetitan": "ServiceTitan", "housecallpro": "Housecall Pro", "housecall pro": "Housecall Pro", "getjobber": "Jobber", "jobber.com": "Jobber",
         "fieldedge": "FieldEdge", "servicefusion": "Service Fusion", "workiz": "Workiz", "kickserv": "Kickserv", "gorilladesk": "GorillaDesk",
         "successware": "Successware", "fieldpulse": "FieldPulse", "markate": "Markate", "serviceminder": "ServiceMinder", "joblogic": "Joblogic",
         "calendly.com": "Calendly", "acuityscheduling": "Acuity Scheduling", "squareup.com/appointments": "Square Appointments", "setmore": "Setmore",
         "vagaro": "Vagaro", "mindbodyonline": "Mindbody", "booksy": "Booksy", "schedulicity": "Schedulicity", "simplybook": "SimplyBook.me",
         "zocdoc": "Zocdoc", "nexhealth": "NexHealth", "opentable": "OpenTable", "resy.com": "Resy", "toasttab": "Toast", "clio.com": "Clio",
         "ruby.com": "Ruby (answering service)", "callruby": "Ruby (answering service)", "smith.ai": "Smith.ai (answering service)",
         "answerconnect": "AnswerConnect (answering service)", "patlive": "PATLive (answering service)", "abbyconnect": "Abby Connect (answering service)",
         "goanswer": "AnswerFirst (answering service)", "callrail": "CallRail (call tracking)", "hubspot": "HubSpot", "salesforce": "Salesforce",
         "gohighlevel": "HighLevel", "leadconnectorhq": "HighLevel", "thryv": "Thryv", "nicejob": "NiceJob (reviews)", "angi.com": "Angi listing",
         "homeadvisor": "HomeAdvisor listing", "thumbtack": "Thumbtack listing"}
MAX_PER_DAY = 8


def ensure(con):
    con.executescript(SCHEMA)
    con.commit()


# ------------------------------------------------------------------ website
def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return r.geturl(), ""
        return r.geturl(), r.read(400_000).decode("utf-8", "ignore")


def _text(page):
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _norm_site(w):
    w = str(w or "").strip()
    if not w:
        return ""
    if not re.match(r"^https?://", w, re.I):
        w = "https://" + w
    u = urllib.parse.urlparse(w)
    return f"{u.scheme}://{u.netloc}" if u.netloc and "." in u.netloc else ""


def read_website(site, fetch=_get, log=print):
    """Facts from the homepage plus up to 2 inner pages (about / services / maintenance). Only what the pages say."""
    base = _norm_site(site)
    if not base:
        return {"status": "no website on file"}
    pages, texts, raw_all = [], [], ""
    try:
        final, page = fetch(base)
    except Exception as e:  # noqa: BLE001
        return {"status": f"couldn't open the website ({e.__class__.__name__})", "url": base}
    if not page:
        return {"status": "website didn't return a page", "url": base}
    pages.append(final)
    raw_all += page
    texts.append(_text(page))
    host = urllib.parse.urlparse(final).netloc
    links = []
    for href in re.findall(r'href=["\']([^"\'#]+)', page, re.I):
        u = urllib.parse.urljoin(final, href)
        if urllib.parse.urlparse(u).netloc == host and re.search(r"about|service|maintenance|management|owners|who-we-are|company", u, re.I):
            if u not in links and u.rstrip("/") != final.rstrip("/"):
                links.append(u)
    for u in links[:2]:
        try:
            f2, p2 = fetch(u)
            if p2:
                pages.append(f2); raw_all += p2; texts.append(_text(p2))
        except Exception:  # noqa: BLE001
            pass
    low = raw_all.lower()
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    desc = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', page) or \
        re.search(r'(?is)<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', page)
    body = " ".join(texts)
    units = []
    for m in re.finditer(r"(?i)\b(\d{1,3}(?:,\d{3})+|\d{2,6})\s*\+?\s*(units|doors|homes|rental homes|properties|apartments|residents)\b", body):
        snippet = body[max(0, m.start() - 60): m.end() + 40].strip()
        if snippet not in units:
            units.append(snippet)
    phones = []
    for p in re.findall(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", body):
        d = re.sub(r"\D", "", p)
        if d not in [re.sub(r"\D", "", x) for x in phones]:
            phones.append(p.strip())
    return {
        "status": "ok", "url": pages[0], "pages_read": pages,
        "title": _html.unescape(title.group(1)).strip()[:160] if title else "",
        "description": _html.unescape(desc.group(1)).strip()[:300] if desc else "",
        "software": sorted({v for k, v in SOFTWARE.items() if k in low}),
        "tools": sorted({v for k, v in TOOLS.items() if k in low}),
        "online_booking": bool(re.search(r"book (?:online|now|an appointment|a service)|schedule (?:online|service|an appointment|now)|request (?:service|an appointment)", body, re.I)),
        "chat_widget": sorted({v for k, v in CHAT.items() if re.search(r"\b" + re.escape(k), low)}),
        "says_24_7": bool(re.search(r"24\s*/\s*7|24 hours|around the clock|after[- ]hours|emergency maintenance", body, re.I)),
        "maintenance_form": bool(re.search(r"maintenance request|submit a (work order|request)|report (a|an) (issue|repair)", body, re.I)),
        "owner_pitch": bool(re.search(r"for (property )?owners|owner portal|free rental analysis|rental analysis", body, re.I)),
        "unit_claims": units[:4], "phones": phones[:4],
        "text_sample": body[:3500],
    }


# ------------------------------------------------------------------ JARVIS's own records
def find_prospect(con, who):
    """Match by prospect id, email, company name or website among AI Sidekicks campaign prospects. Returns a list of candidates."""
    w = str(who or "").strip().lower()
    if not w:
        return []
    out = []
    for r in con.execute("SELECT prospect_id, company, to_addr, info, campaign_id, subject, body, status, updated FROM hq_campaign_items ORDER BY id DESC"):
        info = {}
        try:
            info = json.loads(r["info"] or "{}")
        except ValueError:
            pass
        hay = " ".join(str(x or "").lower() for x in (r["prospect_id"], r["company"], r["to_addr"], info.get("website")))
        if w in hay or (len(w) > 3 and all(t in hay for t in re.findall(r"[a-z0-9]{3,}", w) if t not in ("the", "llc", "inc", "management", "property", "properties"))):
            out.append({**dict(r), "info": info})
    seen, uniq = set(), []
    for o in out:  # one row per prospect (the newest campaign item)
        if o["prospect_id"] not in seen:
            seen.add(o["prospect_id"]); uniq.append(o)
    return uniq


def own_records(con, p):
    """What JARVIS already has on this prospect."""
    from . import hq
    info = p.get("info") or {}
    reps = [{"at": r["at"], "class": r["class"] or "not read yet", "text": (r["text"] or "")[:800],
             "auto_reply": bool(re.search(r"ticket number|has been received|auto(matic)?[- ]?reply|out of (the )?office", r["text"] or "", re.I))}
            for r in hq.email_replies(con, p["prospect_id"], 10)["replies"]] if p.get("prospect_id") else []
    return {"company": p.get("company") or info.get("company"), "email": p.get("to_addr") or info.get("email"),
            "website": info.get("website"), "city": info.get("city"), "state": info.get("state"), "units_from_search": info.get("units"),
            "phone_from_search": info.get("phone"), "how_we_found_them": info.get("source"), "search_notes": info.get("notes"),
            "our_email": {"subject": p.get("subject"), "body": (p.get("body") or "")[:1500], "status": p.get("status"), "updated": p.get("updated")},
            "replies": reps}


# ------------------------------------------------------------------ the brief
SYSTEM = ("You prepare a sales rep for a discovery call. The rep is Mola, founder of AI Sidekicks, which builds custom AI assistants and "
          "custom CRMs for small and midsize businesses (any industry: home services, property management, clinics, law firms and more): assistants "
          "that answer calls and messages, book and schedule work, follow up on leads and update the CRM, with a person in the loop. "
          "Be concrete and brief. Use only the facts supplied or found in your searches, with sources. If something isn't known, say unknown; "
          "never guess sizes, names, software or numbers.")
OUTPUT = ('{"who_they_are": "2 sentences: what they do, where, who for", "size": "doors/units with source, or unknown", '
          '"software": "software they use (field service, booking, CRM, property software), or unknown", '
          '"current_setup": [{"area": "phones / after-hours / booking / scheduling / CRM / follow-up / reviews", "today": "what they use or do now", '
          '"source": "URL, or \'their website code\' for tools detected there"}] (only what the facts or your searches show; empty if nothing found), '
          '"contact": "who we emailed and how they responded", '
          '"reviews": "what public reviews say about responsiveness/maintenance/phones, with source URLs, or none found", '
          '"likely_pain": ["2-4 pains tied to evidence (e.g. reviews mention unanswered calls)"], '
          '"questions": ["4 open questions to ask on the call"], "pitch_angle": "1-2 sentences: which AI Sidekicks offer to lead with and why", '
          '"watch_outs": ["0-3 risks or objections to expect"], "unknowns": ["what we still do not know"], "sources": ["urls"], "needs_info": []}')


def build(con, who=None, prospect=None, website=None, meeting=None, fetch=_get, use_web=True, log=print):
    """Build (or rebuild) a brief. who = company / email / prospect id; website = a site for someone not in JARVIS yet."""
    from . import agents_ai
    ensure(con)
    p = prospect
    if p is None and who:
        hits = find_prospect(con, who)
        if len(hits) > 1:
            names = sorted({h.get("company") or h.get("to_addr") for h in hits})
            if len(names) > 1:
                return {"status": "needs_input", "question": "Which one? " + "; ".join(names[:5])}
        p = hits[0] if hits else None
    if p is None and not website and not who:
        return {"status": "needs_input", "question": "Who's the call with? A company name, their email or their website."}
    p = p or {"prospect_id": "", "company": who or "", "to_addr": "", "info": {"website": website or ""}}
    if meeting and meeting.get("attendee") and not p.get("to_addr"):
        p["to_addr"] = meeting["attendee"]
    mine = own_records(con, p)
    site = website or mine.get("website") or ""
    if not site and mine.get("email") and "@" in mine["email"]:
        dom = mine["email"].split("@", 1)[1].lower()
        if dom not in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "comcast.net", "verizon.net", "msn.com", "live.com"):
            site = dom  # the business email's own domain is their website
    web = read_website(site, fetch=fetch, log=log) if site else {"status": "no website on file"}
    crm = {}
    try:
        from . import hq
        crm = hq.di_record_brief(con, prospect_id=p.get("prospect_id"), limit=12) if p.get("prospect_id") else {}
    except Exception:  # noqa: BLE001
        crm = {}
    facts = {"jarvis_records": mine, "crm_record": crm, "website": web, "meeting": meeting or {}}
    key = (p.get("prospect_id") or _norm_site(site) or (mine.get("company") or who or "").lower())[:200]
    try:
        out, meta = agents_ai.ask(con, agent="ae", kind="ae.precall_brief", task_id=f"precall:{key}", business="demigod", tier="fast",
                                  system=SYSTEM, instructions="Write the pre-call brief for the call described in the data. Fill current_setup with what they use today "
                                  "for phones, after-hours, booking, scheduling, CRM and follow-up: start from website.tools, website.online_booking and the CRM facts, "
                                  "then search for public reviews (responsiveness, missed calls, scheduling) and company size, only if the company and city are known." if use_web else "Write the pre-call brief from the data only.",
                                  data=facts, output_spec=OUTPUT, max_tokens=1600, web_search=3 if use_web and (mine.get("company") or web.get("title")) else 0)
    except agents_ai.CapReached as e:
        _store(con, key, p, mine, site, meeting, facts, None, 0, "waiting", str(e))
        return {"status": "waiting", "summary": str(e), "facts": facts}
    if not isinstance(out, dict) or not out.get("who_they_are"):
        _store(con, key, p, mine, site, meeting, facts, None, meta.get("cost", 0), "failed", "The AI's answer didn't come back as a brief.")
        return {"status": "failed", "summary": "The brief didn't come back in the right shape; nothing saved as a brief.", "facts": facts}
    out["sources"] = sorted(set((out.get("sources") or []) + (meta.get("sources") or []) + ([web["url"]] if web.get("url") else [])))[:15]
    _store(con, key, p, mine, site, meeting, facts, out, meta.get("cost", 0), "ready", "")
    return {"status": "ok", "company": mine.get("company") or web.get("title") or who, "brief": out, "cost": meta.get("cost"),
            "summary": f"Brief ready for {mine.get('company') or web.get('title') or who}."}


def _store(con, key, p, mine, site, meeting, facts, brief, cost, status, err):
    con.execute("""INSERT INTO hq_precall(key,company,email,website,prospect_id,meeting_start,created,facts,brief,cost,status,error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET company=excluded.company, email=excluded.email,
                   website=excluded.website, meeting_start=COALESCE(excluded.meeting_start, meeting_start), created=excluded.created,
                   facts=excluded.facts, brief=COALESCE(excluded.brief, brief), cost=COALESCE(cost,0)+excluded.cost, status=excluded.status, error=excluded.error""",
                (key, mine.get("company") or "", mine.get("email") or "", site, p.get("prospect_id") or "", (meeting or {}).get("start"),
                 db.now(), json.dumps(facts, default=str), json.dumps(brief) if brief else None, cost, status, err))
    con.commit()


def get(con, who):
    ensure(con)
    w = f"%{str(who or '').strip().lower()}%"
    r = con.execute("""SELECT * FROM hq_precall WHERE lower(company) LIKE ? OR lower(email) LIKE ? OR lower(website) LIKE ? OR prospect_id=? OR key=?
                       ORDER BY created DESC LIMIT 1""", (w, w, w, who, who)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["brief"] = json.loads(d["brief"]) if d.get("brief") else None
    d["facts"] = json.loads(d["facts"]) if d.get("facts") else None
    return d


def list_briefs(con, limit=20):
    ensure(con)
    out = []
    for r in con.execute("SELECT key, company, email, website, meeting_start, created, status, error, cost FROM hq_precall ORDER BY created DESC LIMIT ?", (int(limit),)):
        out.append(dict(r))
    return {"briefs": out}


def for_voice(con, who):
    """'Brief me on X': the saved brief if it's fresh (3 days), otherwise build one now."""
    have = get(con, who) if who else None
    if have and have.get("brief") and have["created"] >= (datetime.now() - timedelta(days=3)).isoformat():
        return {"status": "ok", "company": have["company"], "brief": have["brief"], "built": have["created"]}
    return build(con, who=who)


# ------------------------------------------------------------------ automatic: booked meetings and warm replies
def tick(con, cfg=None, fetch=_get, log=print, now=None):
    """Build briefs for AI Sidekicks meetings in the next 36 hours and for prospects who replied wanting a call. A few a day at most."""
    ensure(con)
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    made = int(db.kv_get(con, "precall_made_" + day) or 0)
    if made >= MAX_PER_DAY:
        return 0
    todo = []
    for r in con.execute("SELECT id, params FROM hq_tasks WHERE kind='ae.book_meeting' AND status IN ('completed','awaiting_approval','queued','waiting')"):
        pr = json.loads(r["params"] or "{}")
        st = str(pr.get("start") or "")
        if st and now.isoformat()[:16] <= st[:16] <= (now + timedelta(hours=36)).isoformat()[:16]:
            todo.append(("meeting", pr))
    for r in con.execute("SELECT DISTINCT prospect_id FROM hq_email_replies WHERE class IN ('interested','meeting') AND prospect_id IS NOT NULL"):
        todo.append(("reply", {"prospect_id": r["prospect_id"]}))
    n = 0
    for why, pr in todo:
        pid = pr.get("prospect_id") or ""
        hits = find_prospect(con, pid or pr.get("attendee") or pr.get("company") or "")
        p = hits[0] if hits else None
        key = (p or {}).get("prospect_id") or pid or (pr.get("attendee") or pr.get("company") or "").lower()
        if not key:
            continue
        have = con.execute("SELECT status, created FROM hq_precall WHERE key=?", (key,)).fetchone()
        if have and have["status"] in ("ready", "failed") and have["created"] >= (now - timedelta(days=3)).isoformat():
            continue
        if made + n >= MAX_PER_DAY:
            break
        res = build(con, who=None if p else (pr.get("company") or pr.get("attendee")), prospect=p, meeting=pr if why == "meeting" else None, fetch=fetch, log=log)
        n += 1
        if res.get("status") == "waiting":
            break  # AI cap reached; try tomorrow
        if res.get("status") == "ok":
            log(f"[{now:%H:%M}] Pre-call brief ready: {res.get('company')}" + (f" (call {pr.get('start')})" if why == "meeting" else " (replied wanting a call)"))
        if why == "meeting" and res.get("status") == "ok":  # a demo page only once a call is actually booked
            try:
                from . import demo_page
                demo_made = int(db.kv_get(con, "demo_made_" + day) or 0)
                if demo_made < demo_page.MAX_PER_DAY:
                    d = demo_page.build(con, prospect_id=(p or {}).get("prospect_id") or pid, company=res.get("company") or pr.get("company"), meeting=pr, log=log)
                    if d.get("status") == "ok" and not d.get("existing"):
                        db.kv_set(con, "demo_made_" + day, str(demo_made + 1))
            except Exception as e:  # noqa: BLE001  (the brief still counts)
                log(f"[{now:%H:%M}] Demo page skipped: {e}")
    if n:
        db.kv_set(con, "precall_made_" + day, str(made + n))
    return n
