"""Pre-call brief for AI Sidekicks sales calls: JARVIS's records + the prospect's website + reviews -> one page, built automatically
before booked meetings and warm replies. No network: the website and the AI are faked. Run: python tests/test_precall.py"""
import json, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, agents_ai, precall
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

HOME = """<html><head><title>Harbor Point Property Management | Boston Rentals</title>
<meta name="description" content="Full-service residential property management in Greater Boston since 2004."></head><body>
<script src="https://harborpoint.appfolio.com/connect/portal.js"></script><script>window.tawk_to_id=1; // tawk.to</script>
<h1>We manage over 1,200 units across Greater Boston</h1><p>Call (617) 555-0142. Tenants: submit a maintenance request online.</p>
<a href="/about-us">About</a> <a href="/owners">Owners</a> <a href="https://facebook.com/x">fb</a></body></html>"""
ABOUT = "<html><body><h2>About</h2><p>Family owned. Emergency maintenance line answered 24/7. Free rental analysis for owners.</p></body></html>"
fetched = []
def site(url, timeout=15):
    fetched.append(url)
    if url.rstrip("/").endswith("harborpoint.com"):
        return "https://harborpoint.com/", HOME
    if "about-us" in url:
        return url, ABOUT
    if "owners" in url:
        return url, "<html><body>Owner portal</body></html>"
    raise OSError("unreachable")

print("Reading their website (facts only)")
w = precall.read_website("harborpoint.com", fetch=site)
ok(w["status"] == "ok" and w["title"].startswith("Harbor Point") and "Greater Boston since 2004" in w["description"], "title and description")
ok(w["software"] == ["AppFolio"] and w["chat_widget"] == ["tawk.to"], f"property software and chat widget from the page's own code ({w['software']}, {w['chat_widget']})")
ok(any("1,200 units" in u for u in w["unit_claims"]) and "(617) 555-0142" in w["phones"], "unit claim quoted in context, listed phone")
ok(w["says_24_7"] and w["maintenance_form"] and w["owner_pitch"], "24/7 line, online maintenance form and owner pitch found (about page read too)")
ok(len(w["pages_read"]) == 3 and not any("facebook" in u for u in fetched), "reads the homepage plus 2 inner pages on their own site, never other sites")
ok(precall.read_website("", fetch=site)["status"] == "no website on file" and precall.read_website("nowhere.com", fetch=site)["status"].startswith("couldn't open"),
   "no website / dead website: said plainly")

print("Building the brief")
con = hq.connect()
precall.ensure(con)
info = {"company": "Harbor Point Property Management", "website": "harborpoint.com", "city": "Boston", "state": "MA", "units": "", "notes": "Residential PM, Back Bay",
        "source": "https://harborpoint.com/contact", "email": "dana@harborpoint.com"}
con.execute("""INSERT INTO hq_campaign_items(campaign_id,prospect_id,company,to_addr,subject,body,status,updated,info) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("c1", "dpw_harbor", "Harbor Point Property Management", "dana@harborpoint.com", "After-hours calls at Harbor Point", "Hi Dana, ...", "sent", "2026-11-09", json.dumps(info)))
con.execute("""INSERT INTO hq_campaign_items(campaign_id,prospect_id,company,to_addr,status,updated,info) VALUES(?,?,?,?,?,?,?)""",
            ("c1", "dpw_mediate", "Mediate Management", "clientservices@mediate-mgmt.example", "sent", "2026-11-09", json.dumps({"company": "Mediate Management", "website": ""})))
con.execute("""INSERT INTO hq_email_replies(email_task,prospect_id,message_id,from_addr,at,text,class) VALUES(?,?,?,?,?,?,?)""",
            ("t1", "dpw_mediate", "m1", "clientservices@mediate-mgmt.example", "2026-11-09T19:56", "Thank you for your email! Your request (Ticket Number #8848) has been received", "other"))
con.commit()

GOOD = {"who_they_are": "Harbor Point manages residential rentals in Greater Boston.", "size": "Over 1,200 units (their homepage).", "software": "AppFolio",
        "contact": "Emailed Dana on Nov 9; no reply yet.", "reviews": "Google reviewers mention slow callbacks (https://g.co/x).", "likely_pain": ["Callbacks"],
        "questions": ["Who answers after 6pm?", "How many calls a week go to voicemail?", "How do work orders get into AppFolio?", "What did the last vendor miss?"],
        "pitch_angle": "Lead with the after-hours phone agent writing work orders into AppFolio.", "watch_outs": ["They already advertise a 24/7 line"],
        "unknowns": ["Call volume"], "sources": ["https://g.co/x"], "needs_info": []}
class FakeAI:
    def __init__(s): s.calls = []; s.messages = s; s.reply = json.dumps(GOOD)
    def create(s, **kw):
        s.calls.append(kw)
        return NS(content=[NS(type="text", text=s.reply, citations=[NS(url="https://www.google.com/maps/place/harbor")])], stop_reason="end_turn",
                  usage=NS(input_tokens=6000, output_tokens=700, server_tool_use=NS(web_search_requests=2)))
fake = FakeAI(); agents_ai.CLIENT = fake
r = precall.build(con, who="harbor point", fetch=site)
ok(r["status"] == "ok" and r["brief"]["questions"][0] == "Who answers after 6pm?" and r["company"] == "Harbor Point Property Management", "brief built from the name he said")
sent = json.loads(fake.calls[0]["messages"][0]["content"].split("<data>")[1].split("</data>")[0])
ok(sent["jarvis_records"]["our_email"]["subject"] == "After-hours calls at Harbor Point" and sent["website"]["software"] == ["AppFolio"],
   "the AI gets our email and the website facts as data")
ok(fake.calls[0]["model"] == "claude-haiku-5-5" and fake.calls[0]["tools"][0]["max_uses"] == 3, "fast model, at most 3 review searches")
ok("https://www.google.com/maps/place/harbor" in r["brief"]["sources"] and "https://harborpoint.com/" in r["brief"]["sources"], "sources include the review link and their site")
ok(0 < r["cost"] < 0.1 and agents_ai.spent(con) == r["cost"], f"priced and counted against the daily AI cap (${r['cost']:.4f})")
g = precall.get(con, "Harbor")
ok(g and g["status"] == "ready" and g["brief"]["size"].startswith("Over 1,200"), "saved, found again by name")
n = len(fake.calls)
v = precall.for_voice(con, "harbor point")
ok(v["status"] == "ok" and len(fake.calls) == n, "'brief me on Harbor Point' reads the saved brief (no new AI cost)")
m = precall.build(con, who="mediate", fetch=site)
med = json.loads(fake.calls[-1]["messages"][0]["content"].split("<data>")[1].split("</data>")[0])
ok(med["jarvis_records"]["replies"][0]["auto_reply"] is True, "an auto-responder ticket is marked as an auto-reply, not a real response")
ok(any("mediate-mgmt.example" in u for u in fetched), "no website on file: the business email's domain is used")
ok(precall.build(con, who="", fetch=site)["status"] == "needs_input", "no name: asks who the call is with")
fake.reply = "Sorry, I can't."
bad = precall.build(con, website="harborpoint.com", who="Harbor Point PM (new)", fetch=site)
ok(bad["status"] == "failed", "an answer that isn't a brief is never saved as one")
fake.reply = json.dumps(GOOD)

print("Automatic: booked meetings and warm replies")
soon = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M")
far = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
con.execute("DELETE FROM hq_precall"); con.commit()
for i, (st, pid, who) in enumerate([(soon, "dpw_harbor", "dana@harborpoint.com"), (far, "dpw_mediate", "x@mediate-mgmt.example")]):
    con.execute("INSERT INTO hq_tasks(id,kind,agent,status,params,created,updated,title) VALUES(?,?,?,?,?,?,?,?)",
                (f"tm{i}", "ae.book_meeting", "ae", "completed", json.dumps({"attendee": who, "start": st, "end": st, "title": "Intro", "prospect_id": pid}), db.now(), db.now(), "Book"))
con.commit()
n = len(fake.calls)
made = precall.tick(con, fetch=site, log=lambda *a: None)
ok(made == 1 and len(fake.calls) == n + 2 and precall.get(con, "dpw_harbor")["meeting_start"] == soon, "a meeting in the next 36 hours gets a brief (plus a demo page attempt); one 5 days out waits")
ok(con.execute("SELECT COUNT(*) FROM hq_demo_pages").fetchone()[0] == 1, "the demo page is tried only for the booked call")
ok(precall.tick(con, fetch=site, log=lambda *a: None) == 0, "not rebuilt every 15 minutes")
con.execute("UPDATE hq_email_replies SET class='meeting'"); con.commit()
ok(precall.tick(con, fetch=site, log=lambda *a: None) == 1 and precall.get(con, "dpw_mediate"), "a reply asking for a call gets a brief right away")
brief = hq.morning_brief(con, {})
call = [c for c in brief["demigod"]["calls"] if c["with"] == "dana@harborpoint.com"]
ok(call and call[0]["brief_ready"] is True, "the morning brief shows the call with 'brief ready'")
agents_ai.CLIENT = None

print("Wired in")
ok({"precall_list", "precall_get"} <= hq.READ_OPS and "precall_build" in hq.WRITE_OPS, "the JARVIS app can list, read and request briefs")
src = (ROOT / "voice" / "jarvis_voice.py").read_text(encoding="utf-8")
ok('("precall_brief"' in src and "precall.for_voice" in src, "by voice: 'Hey Jarvis, brief me on Harbor Point'")
rs = (ROOT / "run.py").read_text(encoding="utf-8")
ok("maybe_start_precall(cfg, scrape_state)" in rs and 'args.cmd == "brief"' in rs, "runs in the background every 15 minutes, and on demand: python run.py brief <name>")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
