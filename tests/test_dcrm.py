"""AI Sidekicks CRM (milestone 1): one record per business, many contacts, sourced facts, explainable scores, evidence-gated
17-stage pipeline, and what JARVIS already has (searches, campaigns, the app's prospects) pulled in. No network.
Run: python tests/test_dcrm.py"""
import json, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, dcrm
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

def dns(url):  # fake DNS-over-HTTPS: deadbiz.example has no mail server
    dom = url.split("name=")[1].split("&")[0]
    if dom == "deadbiz.example":
        return json.dumps({"Status": 3})
    return json.dumps({"Status": 0, "Answer": [{"type": 15, "data": "10 mx." + dom}]})

con = hq.connect(); dc = dcrm.connect()
print("One record per business")
a, created, _ = dcrm.upsert(dc, {"company": "Acme Heating & Cooling LLC", "website": "https://www.acmeheat.com/", "city": "Quincy", "state": "MA",
                                 "email": "info@acmeheat.com", "email_source": "https://acmeheat.com/contact", "industry": "home services",
                                 "notes": "Family HVAC, 24/7 emergency service, free estimates, 12 technicians", "source": "https://acmeheat.com/about"}, fetch=dns)
ok(created, "a new business is created")
b, created2, how = dcrm.upsert(dc, {"company": "ACME HEATING AND COOLING", "website": "acmeheat.com/services", "email": "sales@acmeheat.com",
                                    "email_source": "https://acmeheat.com/services", "phone": "(617) 555-0100", "source": "https://acmeheat.com/services"}, fetch=dns)
ok(b == a and not created2 and how == "domain", f"same website, different email: merged, not duplicated (matched on {how})")
c, _, how = dcrm.upsert(dc, {"company": "Acme HVAC", "phone": "617.555.0100", "source": "https://www.yelp.com/biz/acme"}, fetch=dns)
ok(c == a and how == "phone_key", "same phone written differently: merged")
d, _, how = dcrm.upsert(dc, {"company": "Acme Heating and Cooling", "city": "Quincy"}, fetch=dns)
ok(d == a and how == "name + city", "same name in the same city: merged")
e, created3, _ = dcrm.upsert(dc, {"company": "Acme Heating and Cooling", "city": "Worcester"}, fetch=dns)
ok(created3 and e != a, "same name in another city is a different business")
det = dcrm.detail(dc, a)
ok(len(det["contacts"]) == 2 and {x["email"] for x in det["contacts"]} == {"info@acmeheat.com", "sales@acmeheat.com"}, "both emails kept as contacts of one business")
ok(dc.execute("SELECT COUNT(*) FROM dc_businesses").fetchone()[0] == 2, "2 records total, not 5")

print("Where each fact came from")
ok(all(f["source_url"] for f in det["facts"] if f["field"] in ("website", "note")) and {f["grade"] for f in det["facts"]} <= {"public", "third_party"}, "every fact has a grade and its source page")
cls = {x["email"]: x["email_class"] for x in det["contacts"]}
ok(cls["info@acmeheat.com"] == "public_unverified" and "domain accepts mail" in det["contacts"][0]["verify_note"],
   "an email published on their site = public, unverified (the domain accepts mail; no paid verifier claimed)")
x, _, _ = dcrm.upsert(dc, {"company": "Dead Biz", "email": "info@deadbiz.example", "email_source": "https://deadbiz.example/contact"}, fetch=dns)
ok(dcrm.detail(dc, x)["contacts"][0]["email_class"] == "invalid", "an email whose domain has no mail server is marked invalid")
y, _, _ = dcrm.upsert(dc, {"company": "Listing Co", "email": "owner@listingco.com"}, fetch=dns)
ok(dcrm.detail(dc, y)["contacts"][0]["email_class"] == "third_party", "an email with no source page = third-party")
dcrm.add_contact(dc, y, "john@listingco.com", email_class="inferred"); dc.commit(); dcrm.score(dc, y)
ok("inferred" not in json.dumps(dcrm.detail(dc, y)["score_detail"]["parts"]["contact"]) and dcrm.detail(dc, y)["score_detail"]["parts"]["contact"]["points"] == 8,
   "an inferred email never counts as a way to reach them")

fb, _, _ = dcrm.upsert(dc, {"company": "Social Co", "email": "info@socialco.com", "email_source": "https://www.facebook.com/socialco"}, fetch=dns)
ok(dcrm.detail(dc, fb)["contacts"][0]["email_class"] == "third_party", "an email found on Facebook (not their own site) = third-party")
print("Explainable scores")
sd = dcrm.detail(dc, a)
ok(sd["priority"] in ("High", "Medium") and sd["score_detail"]["parts"]["inquiries"]["evidence"] == "strong" and "24/7" in sd["score_detail"]["parts"]["inquiries"]["why"],
   f"Acme scores on evidence (24/7, estimates, 12 technicians): {sd['score']} {sd['priority']}")
ok(sd["score_detail"]["parts"]["size"]["points"] == 15 and "12 technicians" in sd["score_detail"]["parts"]["size"]["why"], "size from the record, with the words that justify it")
z, _, _ = dcrm.upsert(dc, {"company": "Bare Plumbing", "industry": "plumbing"}, fetch=dns)
zd = dcrm.detail(dc, z)
ok(zd["priority"] == "Research required" and zd["score"] < 30, f"industry alone doesn't score: {zd['score']}, {zd['priority']}")
ok(all("why" in p and "evidence" in p for p in zd["score_detail"]["parts"].values()), "every part of every score says why and how strong the evidence is")

print("Stages need evidence")
try:
    dcrm.move(dc, a, "Contacted", by="sdr"); refused = False
except ValueError as er:
    refused = "No evidence" in str(er)
ok(refused, "the SDR can't mark Acme contacted without a sent email")
try:
    dcrm.move(dc, a, "Negotiation", by="owner"); refused2 = False
except ValueError:
    refused2 = True
ok(refused2, "even you need to say what happened")
r = dcrm.move(dc, a, "Negotiation", by="owner", note="Talked to the owner at a trade show; wants pricing")
ok(r["status"] == "ok" and r["evidence"]["owner_note"].startswith("Talked"), "your note is saved as the evidence")
try:
    dcrm.move(dc, z, "Lost", by="owner"); refused3 = False
except ValueError:
    refused3 = True
ok(refused3, "Lost needs a reason")
h = dcrm.detail(dc, a)["history"]
ok(h[0]["to_stage"] == "Newly Discovered" and h[-1]["to_stage"] == "Negotiation" and all(x["at"] for x in h), "every move is timestamped in the history")

print("What JARVIS already has")
info = {"company": "Harbor Point PM", "website": "harborpoint.com", "city": "Boston", "state": "MA", "email": "dana@harborpoint.com",
        "source": "https://harborpoint.com/contact", "notes": "1,200 units, online maintenance requests", "industry": "property management"}
con.execute("INSERT INTO hq_campaign_items(campaign_id,prospect_id,company,to_addr,status,updated,info) VALUES(?,?,?,?,?,?,?)",
            ("c1", "dpw_harbor", "Harbor Point PM", "dana@harborpoint.com", "sent", "2026-11-09", json.dumps(info)))
con.execute("INSERT INTO hq_campaign_items(campaign_id,prospect_id,company,to_addr,status,updated,info) VALUES(?,?,?,?,?,?,?)",
            ("c1", "dpw_mediate", "Mediate Management", "clientservices@mediate-mgmt.example", "sent", "2026-11-09", json.dumps({"company": "Mediate Management", "website": "mediate-mgmt.example", "source": "https://mediate-mgmt.example"})))
con.commit()
for pid, to in (("dpw_harbor", "dana@harborpoint.com"), ("dpw_mediate", "clientservices@mediate-mgmt.example")):
    t = hq.create_task(con, "sdr.send_email", {"to": to, "subject": "Hi", "body": "Hi ... reply no thanks and I won't follow up.", "prospect_id": pid, "company": pid}, division="demigod")
    hq.approve(con, t["id"], t["params_hash"]); hq.email_sent(con, t["id"], "m_" + pid, "th_" + pid)
con.execute("INSERT INTO hq_email_replies(email_task,prospect_id,message_id,from_addr,at,text,class) VALUES(?,?,?,?,?,?,?)",
            ("x", "dpw_mediate", "r1", "clientservices@mediate-mgmt.example", "2026-11-09T19:56", "Thank you for your email! Your request (Ticket Number #8848) has been received", "other"))
con.commit()
s1 = dcrm.sync(dc, fetch=dns, do_enrich=False)
hb = [x for x in dcrm.businesses(dc)["businesses"] if x["name"] == "Harbor Point PM"][0]
md = [x for x in dcrm.businesses(dc)["businesses"] if x["name"] == "Mediate Management"][0]
ok(s1["campaign_prospects"] == 2 and hb["stage"] == "Contacted" and md["stage"] == "Contacted", f"campaign prospects come in already Contacted, from the sent emails ({hb['stage']})")
ok(md["stage"] != "Replied", "an auto-responder ticket doesn't count as a reply")
ok(not hb["next_action"] or "Draft a first email" not in hb["next_action"], "a business already emailed isn't told to 'draft a first email'")
con.execute("INSERT INTO hq_email_replies(email_task,prospect_id,message_id,from_addr,at,text,class) VALUES(?,?,?,?,?,?,?)",
            ("x", "dpw_harbor", "r2", "dana@harborpoint.com", "2026-11-12T10:00", "Sure, happy to talk Thursday.", "meeting"))
con.commit()
dcrm.sync(dc, fetch=dns, do_enrich=False)
hb = dcrm.detail(dc, hb["id"])
ok(hb["stage"] == "Interested" and hb["history"][-1]["by"] == "system" and hb["history"][-1]["evidence"]["class"] == "meeting",
   "a real 'let's talk' reply moves Harbor Point to Interested, with the reply as evidence")
ok(hb["outreach"] and hb["replies"], "the record shows its sent emails and replies")
s2 = dcrm.sync(dc, fetch=dns, do_enrich=False)
ok(s2["campaign_prospects"] == 0 and s2["new"] == 0, "running the sync again adds nothing")
imp = dcrm.ingest_app(dc, [{"id": "ap1", "company": "Bay Rentals", "contact": "Dana Q", "email": "dana@bayrentals.com", "industry": "property management",
                           "pain": "missed after-hours calls", "stage": "Proposal", "nextAction": "Send revised quote", "nextDate": "2026-11-20"},
                          {"id": "ap2", "company": "Harbor Point PM", "email": "dana@harborpoint.com", "stage": "Lead"}], fetch=dns)
br = [x for x in dcrm.businesses(dc)["businesses"] if x["name"] == "Bay Rentals"][0]
ok(imp["app_prospects"] == 2 and br["stage"] == "Proposal Sent" and br["next_action"] == "Send revised quote" and br["app_id"] == "ap1",
   "the app's prospects come in with their stage (Proposal -> Proposal Sent) and next action, marked as imported")
ok(len([x for x in dcrm.businesses(dc, {"include_closed": True})["businesses"] if x["name"] == "Harbor Point PM"]) == 1 and dcrm.detail(dc, hb["id"])["stage"] == "Interested",
   "an app prospect that's already in the CRM merges, and never moves a record backwards")

print("Website research (free)")
SITE = """<html><head><title>Bolt Plumbing | Medford</title><meta name="description" content="Family plumbing company serving Medford since 1998."></head>
<body>Book online or request a free estimate. 24/7 emergency service. Our 18 technicians cover 3 locations. <a href="/about">About</a></body></html>"""
def web(url, timeout=15):
    if "boltplumb" in url:
        return ("https://boltplumb.com/", SITE) if not url.rstrip("/").endswith("about") else (url, "<html><body>Licensed and insured. Financing available.</body></html>")
    raise OSError("down")
bp, _, _ = dcrm.upsert(dc, {"company": "Bolt Plumbing", "website": "boltplumb.com", "city": "Medford", "industry": "plumbing",
                            "email": "office@boltplumb.com", "email_source": "https://boltplumb.com/contact"}, fetch=dns)
before = dcrm.detail(dc, bp)
n = dcrm.enrich(dc, 50, fetch=web)
after = dcrm.detail(dc, bp)
ok(n >= 1 and after["researched_at"] and any(f["field"] == "inquiries" and "free estimate" in f["value"] for f in after["facts"]),
   "the SDR reads their website and records what it says, with the page as the source")
ok(after["score"] > before["score"] and after["score_detail"]["parts"]["size"]["points"] == 15 and after["priority"] in ("High", "Medium"),
   f"evidence from the site raises the score ({before['score']} -> {after['score']}, {after['priority']}); 18 technicians fits 2-250")
ok(dcrm.enrich(dc, 50, fetch=web) == 0, "each website is read once")
print("Wired in")
ok({"di_crm_summary", "di_businesses", "di_business"} <= hq.READ_OPS and {"di_move_stage", "di_import_app", "di_business_upsert"} <= hq.WRITE_OPS, "the app can read and edit the CRM")
r = hq.handle(con, {}, {"op": "di_move_stage", "id": br["id"], "stage": "Won", "note": "Signed today"})
ok(r["stage"] == "Won", "you move a stage from the app with a note")
brief = hq.morning_brief(con, {})
ok(brief["demigod_crm"] and brief["demigod_crm"]["total"] >= 6, "the morning brief counts the CRM")
src = (ROOT / "engine" / "duties.py").read_text(encoding="utf-8")
ok("dcrm.sync" in src, "the SDR syncs the CRM every 30 minutes")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
