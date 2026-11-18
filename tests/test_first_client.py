"""First paying client workflow: outreach claims check and follow-up rules, bounces, reply benchmark, discovery-to-proposal,
the demo page for a booked call, and "what they use today" in the pre-call brief. No network, no AI spend (the AI is faked).
Run: python tests/test_first_client.py"""
import json, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, dcrm, agents_ai, duties, precall, demo_page, gmail
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
def raises(fn, text=""):
    try:
        fn()
    except ValueError as e:
        return text.lower() in str(e).lower()
    return False
quiet = lambda *a, **k: None
con = hq.connect(); dc = dcrm.connect()
hq.set_email_footer(con, "Ademola Faleye, AI Sidekicks · 1 Example St, Springfield, MA 01103")

calls = []
def fake_ask(replies):
    """Stands in for agents_ai.ask: returns the queued replies in order and records each prompt."""
    q = list(replies)
    def ask(con_, **kw):
        calls.append(kw)
        out = q.pop(0) if q else q
        return out, {"model": "fake", "input_tokens": 10, "output_tokens": 10, "cost": 0.01, "searches": 0, "sources": []}
    return ask
real_ask = agents_ai.ask

print("Claims check on outreach")
ok(agents_ai.claims_found("We're the best AI agency in Boston") and agents_ai.claims_found("Guaranteed to double your bookings")
   and agents_ai.claims_found("Cut missed calls by 40% fewer") and agents_ai.claims_found("a leading provider"), "superlatives, guarantees, multiples and percentage results are caught")
ok(not agents_ai.claims_found("What's the best time for a 15-minute call?\nIf it's not relevant, reply 'no thanks'.") and not agents_ai.claims_found("24x7 coverage") and not agents_ai.claims_found("Best,\nAdemola"),
   "ordinary wording isn't ('best time', 24x7, a 'Best,' sign-off)")
prev = [{"subject": "Custom AI Assistants & CRMs for Bay", "body": "Hi Dana, AI Sidekicks builds custom AI assistants that work alongside a team and give them their time back. An assistant could answer after-hours calls and log maintenance requests."}]
iss = agents_ai.draft_issues("sdr.followup_draft", {"drafts": [{"subject": "Re: x", "body": "Just checking in on my last email. Did you get a chance to look?"}]}, {"previous": prev})
ok(any("filler" in i for i in iss) and any("nudge" in i for i in iss), f"a follow-up with no new reason is caught ({iss})")
iss2 = agents_ai.draft_issues("sdr.followup_draft", {"drafts": [{"subject": "Re: x", "body": prev[0]["body"]}]}, {"previous": prev})
ok(any("repeats an earlier email" in i for i in iss2), "a follow-up that repeats the first email is caught")

good = {"summary": "s", "deliverable": "Follow-up #2 adds a scheduling example drawn from their site; one easy question and the opt-out are included.", "needs_info": [],
        "drafts": [{"channel": "email", "subject": "Re: Custom AI Assistants & CRMs for Bay", "body": "Hi Dana, one more idea: your site says tenants book showings by phone. An assistant could offer open showing times by text and put them on the calendar, with your team approving. Worth a 15-minute look? If it's not relevant, reply 'no thanks'."}]}
bad = json.loads(json.dumps(good)); bad["drafts"][0]["body"] = "Hi Dana, we're the best team for this and guarantee results. If not relevant, reply 'no thanks'."
try:
    calls.clear(); agents_ai.ask = fake_ask([bad, good])
    t = hq.create_task(con, "sdr.followup_draft", {"company": "Bay", "previous": prev, "step": 2}, division="demigod")
    out = agents_ai.EXECUTORS["sdr.followup_draft"](con, {}, hq.get_task(con, t["id"], events=False))
    ok(out["verified"] and len(calls) == 2 and "REVIEW" in calls[1]["instructions"] and "best" in calls[1]["instructions"],
       "a draft with a claim is sent back once automatically, with the problem named, and the clean redraft counts")
    ok(out["result"]["meta"].get("redrafted_for") and abs(out["result"]["meta"]["cost"] - 0.02) < 1e-9, "the redraft is recorded and both calls are priced")
    calls.clear(); agents_ai.ask = fake_ask([bad, bad])
    t2 = hq.create_task(con, "sdr.followup_draft", {"company": "Bay2", "previous": prev, "step": 2}, division="demigod")
    out2 = agents_ai.EXECUTORS["sdr.followup_draft"](con, {}, hq.get_task(con, t2["id"], events=False))
    ok(not out2["verified"] and "claims check" in out2["summary"] and out2["result"]["issues"], "still bad after the redraft: held back, not counted as a draft")
    calls.clear(); agents_ai.ask = fake_ask([good])
    t3 = hq.create_task(con, "research.brief", {"topic": "AI receptionists, the best options"}, division="demigod")
    agents_ai.EXECUTORS["research.brief"](con, {}, hq.get_task(con, t3["id"], events=False))
    ok(len(calls) == 1, "only outreach and marketing drafts are checked (a research brief isn't)")
finally:
    agents_ai.ask = real_ask
spec = agents_ai.SPECS["sdr.followup_draft"]["instructions"]
ok("just checking in" in spec and "Follow-up #2" in spec and "Follow-up #3" in spec and "record" in spec, "follow-up instructions: each one adds something new (#2 a new example from the record, #3 the last note)")
ok(raises(lambda: hq.create_task(con, "sdr.send_email", {"to": "x@acme.com", "subject": "Hi", "body": "We are the #1 agency and guarantee more bookings. Reply 'no thanks' to opt out."}, division="demigod"), "can't back up"),
   "a send with an unsupported claim can't even be queued for approval")
ok(hq.create_task(con, "sdr.send_email", {"to": "y@acme.com", "subject": "Hi", "body": "What's the best time for a short call? Reply 'no thanks' to opt out."}, division="demigod")["id"],
   "an ordinary email still queues")

print("Bounces stop the sequence")
e1 = hq.create_task(con, "sdr.send_email", {"to": "dana@bay.com", "subject": "Hi Bay", "body": "Hi Dana, a short note. Reply 'no thanks' and I won't follow up.", "prospect_id": "dp_bay", "company": "Bay Rentals"}, division="demigod")
hq.approve(con, e1["id"], e1["params_hash"]); hq.email_sent(con, e1["id"], "m_bay1", "th_bay")
e2 = hq.create_task(con, "sdr.send_email", {"to": "sam@reef.com", "subject": "Hi Reef", "body": "Hi Sam, a short note. Reply 'no thanks' and I won't follow up.", "prospect_id": "dp_reef", "company": "Reef Co"}, division="demigod")
hq.approve(con, e2["id"], e2["params_hash"]); hq.email_sent(con, e2["id"], "m_reef1", "th_reef")
old = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
for e, m, th in ((e1, "m_bay1", "th_bay"), (e2, "m_reef1", "th_reef")):
    con.execute("UPDATE hq_tasks SET result=? WHERE id=?", (json.dumps({"message_id": m, "thread_id": th, "sent_at": old}), e["id"]))
con.commit()
ok({d["prospect_id"] for d in hq.followups_due(con)["due"]} == {"dp_bay", "dp_reef"}, "both are due for follow-up #2")
(tmp / "data" / "google_client.json").write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
(tmp / "data" / "gmail_token.json").write_text(json.dumps({"refresh_token": "r"}))
now_ms = str(int(time.time() * 1000))
threads = {"th_bay": [{"id": "b1", "from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "internalDate": now_ms,
                       "text": "Address not found. Your message wasn't delivered to dana@bay.com because the address couldn't be found."}],
           "th_reef": [{"id": "x1", "from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "internalDate": now_ms, "text": "Message delayed, still trying."}]}
real_thread = gmail.thread; gmail.thread = lambda tid: threads.get(tid, [])
try:
    db.kv_set(con, "di_engine_next_reply", "0"); con.commit()
    r = hq.engine_reply_tick(con, {}, log=quiet, pause_s=0, sleep=lambda s: None)
finally:
    gmail.thread = real_thread
ok(r["found"] == 1 and hq.email_bounced(con) == {"dana@bay.com"}, "a 'address not found' notice in the thread is recorded as a bounce (a 'delayed' notice isn't)")
ok({d["prospect_id"] for d in hq.followups_due(con)["due"]} == {"dp_reef"}, "no follow-up to an address that bounced")
ok(raises(lambda: hq.create_task(con, "sdr.send_email", {"to": "dana@bay.com", "subject": "Hi", "body": "Hi again. Reply 'no thanks' to opt out."}, division="demigod"), "bounced"),
   "no new email to it either")
fu = hq.create_task(con, "sdr.followup_draft", {"company": "Reef", "previous": prev, "to": "sam@reef.com", "prospect_id": "dp_reef"}, division="demigod")
hq.email_bounce_add(con, "sam@reef.com", reason="recorded by the owner")
ok(hq.get_task(con, fu["id"], events=False)["status"] == "cancelled", "a bounce cancels anything still queued to that address")
bid, _, _ = dcrm.upsert(dc, {"company": "Bay Rentals", "email": "dana@bay.com", "email_source": "https://bay.com/contact", "website": "https://bay.com"},
                        fetch=lambda u: json.dumps({"Status": 0, "Answer": [{"type": 15}]}))
dcrm.mark_bounces(dc)
ok(dcrm.detail(dc, bid)["contacts"][0]["email_class"] == "invalid", "the CRM marks the bounced contact invalid and rescores")

print("Reply-rate benchmark")
cid = con.execute("SELECT id FROM hq_campaigns LIMIT 1").fetchone()
st = hq.campaign_stats(con, cid[0] if cid else "none")
ok(st["benchmark"]["average_reply_rate"] == 4.5 and "Hunter" in st["benchmark"]["source"] and "not what to expect" in st["benchmark"]["note"] and "bounce_rate" in st and st["enough_to_judge"] is False,
   "campaign stats carry a sourced reference reply rate (not a promise), the bounce rate, and whether there's enough data to judge")

print("Follow-up drafts get the CRM's facts")
pb, _, _ = dcrm.upsert(dc, {"company": "Reef Co", "phone": "617-555-0199", "services": "plumbing, water heaters", "industry": "home services",
                            "notes": "Customers book service online; 24/7 emergency line.", "source": "https://reef.example/services"}, check_email_dns=False)
dc.execute("INSERT OR IGNORE INTO dc_links(business_id,prospect_id) VALUES(?,?)", (pb, "dp_reef2")); dc.commit()
rec = hq.di_record_brief(con, prospect_id="dp_reef2")
ok(rec["name"] == "Reef Co" and any("24/7" in f["fact"] and f["source"].startswith("https://") for f in rec["facts"]), "the record brief has sourced facts")
ok("record" in (ROOT / "engine" / "duties.py").read_text(encoding="utf-8").split("def followups")[1].split("def ")[0], "the 10 AM duty passes them to each follow-up draft")

print("Discovery to proposal")
ok(raises(lambda: hq.handle(con, {}, {"op": "di_proposal", "company": "Reef Co", "notes": "short"}), "call notes"), "it needs real call notes")
hq.handle(con, {}, {"op": "set_di_pricing", "text": "AI receptionist: setup $1,500-$3,000, monthly $300-$600."})
ok(hq.handle(con, {}, {"op": "di_pricing"})["pricing_guide"].startswith("AI receptionist"), "you set your price ranges once")
NOTES = ("Call with Sam (owner), Reef Co. 6 techs. Phones go to Sam's cell after 5pm and he misses calls on jobs. Uses Housecall Pro for jobs. "
         "Wants after-hours calls answered and emergency jobs routed to the on-call tech. Budget not set; wants to start small.")
tp = hq.handle(con, {}, {"op": "di_proposal", "dc_business_id": pb, "notes": NOTES})
ok(tp["kind"] == "ae.discovery_proposal" and tp["level"] == 2 and tp["params"]["company"] == "Reef Co", "a proposal draft is queued for the business (level 2: waits for your review)")
prop = {"summary": "Proposal for Reef Co", "deliverable": "Phase 1 answers after-hours calls and routes emergencies to the on-call tech; Housecall Pro access to confirm.", "needs_info": [],
        "current_setup": [{"area": "after-hours", "today": "calls go to Sam's cell", "source": "call"}],
        "bottlenecks": [{"problem": "missed calls on jobs", "evidence": "he misses calls on jobs", "impact": "not quantified"}],
        "solution": {"summary": "after-hours phone assistant", "pattern": "phone assistant that hands off to staff", "components": [], "human_in_the_loop": ["on-call tech accepts emergencies"], "data_and_access": [], "risks": []},
        "phases": [{"name": "Phase 1", "includes": ["after-hours answering"], "weeks": "2-3", "first_value": "no missed after-hours calls logged"}],
        "pricing": {"setup_low": 1500, "setup_high": 3000, "monthly_low": 300, "monthly_high": 600, "basis": "guide", "source": "owner pricing guide"},
        "proposal": "# Reef Co\nYou told us calls go to your cell after 5pm...", "open_questions": ["Housecall Pro API access?"]}
try:
    calls.clear(); agents_ai.ask = fake_ask([prop])
    out = hq.EXECUTORS["ae.discovery_proposal"](con, {}, hq.get_task(con, tp["id"], events=False))
finally:
    agents_ai.ask = real_ask
sent_data = calls[0]["data"]
ok(out["verified"] and sent_data["record"]["name"] == "Reef Co" and sent_data["pricing_guide"].startswith("AI receptionist") and sent_data["notes"].startswith("Call with Sam"),
   "the draft works from the call notes, the CRM record and your pricing guide")
ins = calls[0]["instructions"]
ok("real time" in ins and "phase 1" in ins.lower() and "no guaranteed results" in ins and "open_questions" in ins and "{offer}" not in ins,
   "it follows the architect method: map today, find the bottleneck, smallest useful phase first, honest pricing source, nothing invented")
ok(set(agents_ai.SPECS["ae.discovery_proposal"]["req"]) == {"bottlenecks", "solution", "pricing", "proposal"}, "a draft missing the plan, price or proposal doesn't count")
ok(agents_ai.price(agents_ai.TIER_MODEL["standard"], 9000, 6000) < duties.budgets(con).get("ae", 1), "one proposal fits inside the account executive's daily AI budget")

print("Demo page for a booked call")
content = {"headline": "Answers Reef Co's after-hours calls", "business_line": "Plumbing and water heaters", "greeting": "Thanks for calling Reef Co, this is the virtual assistant.",
           "scenarios": [{"title": "Burst pipe at night", "caller_need": "water everywhere", "conversation": [{"who": "Caller", "text": "My pipe burst <script>alert(1)</script>"}, {"who": "Assistant", "text": "I'm alerting the on-call tech now."}],
                          "what_happens_next": ["texts the on-call tech", "logs it for the morning"]}],
           "handoffs": ["emergencies go to the on-call tech"], "would_connect_to": [{"system": "Housecall Pro", "why": "create the job", "known": True}],
           "set_up_with_you": ["your hours", "your service area"], "not_included": ["won't guarantee arrival times", "no card numbers"], "needs_info": []}
r = demo_page.build(con, business_id=pb, meeting={"start": "2026-11-14T14:00"}, ask=fake_ask([content]), out_dir=tmp / "demos", log=quiet)
page = Path(r["path"]).read_text(encoding="utf-8")
ok(r["status"] == "ok" and "Reef Co" in page and "Burst pipe" in page and "Housecall Pro" in page, "a one-page demo is built for the business")
ok("<script>" not in page and "&lt;script&gt;" in page and "http" not in page.split("<style>")[0] + page.split("</style>")[1].replace("http-equiv", ""),
   "everything is escaped and the page loads nothing from the internet")
ok("not a live system" in page and "Demo" in page and "set up with you" in page.lower(), "it says plainly it's a demo, and unknowns are 'set up with you'")
ok(demo_page.build(con, business_id=pb, ask=fake_ask([content]), out_dir=tmp / "demos", log=quiet).get("existing"), "it isn't rebuilt (or re-paid) unless you ask")
claimy = json.loads(json.dumps(content)); claimy["headline"] = "The best answering service in Massachusetts"
r2 = demo_page.build(con, company="Claimy Co", ask=fake_ask([claimy]), out_dir=tmp / "demos", log=quiet)
ok(r2["status"] == "failed" and "claim" in r2["summary"], "a demo that makes a claim isn't made")
src = (ROOT / "engine" / "precall.py").read_text(encoding="utf-8")
ok('why == "meeting"' in src.split("def tick")[1] and "demo_page.build" in src.split("def tick")[1], "made automatically only for booked calls (not for every reply)")
ok({"demo_pages", "demo_page"} <= hq.READ_OPS and {"demo_page_build", "di_proposal", "set_di_pricing", "email_bounce_add"} <= hq.WRITE_OPS, "the app and voice can reach all of it")

print("What they use today (pre-call brief)")
SITE = ('<html><head><title>Reef Co Plumbing</title></head><body>Book online 24/7. <script src="https://book.housecallpro.com/widget.js"></script>'
        '<a href="https://calendly.com/reef">Schedule</a> Call (617) 555-0199</body></html>')
w = precall.read_website("reef.example", fetch=lambda u, timeout=15: ("https://reef.example/", SITE), log=quiet)
ok(set(w["tools"]) >= {"Housecall Pro", "Calendly"} and w["online_booking"], f"the site's code shows their booking tools ({w['tools']})")
ok("current_setup" in precall.OUTPUT and "property management" not in precall.SYSTEM.split("(")[0] and "any industry" in precall.SYSTEM, "the brief has a 'what they use today' section and isn't property-management-only")

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
