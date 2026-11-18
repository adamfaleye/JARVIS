"""Morning brief: one read across MolaBucks and AI Sidekicks, from the engine's own records.
Run: python tests/test_morning_brief.py"""
import json, sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, hq
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
cfg = {"timezone": "America/New_York"}
con = hq.connect()
t = date.today(); yday = (t - timedelta(days=1)).isoformat(); nowi = datetime.now().isoformat(timespec="seconds")

print("Quiet morning")
b = hq.morning_brief(con, cfg)
ok(b["headline"] == "Nothing needs you this morning.", f"nothing on file reads as a quiet morning ({b['headline']})")
ok(b["engine"]["loop_running"] is False, "the brief says when the engine loop isn't running")

print("A busy morning")
put = lambda kind, d: con.execute("INSERT INTO crm_items(kind,id,data,ts) VALUES(?,?,?,?)", (kind, d["id"], json.dumps(d), nowi))  # noqa: E731
put("deals", {"id": "d1", "address": "12 Oak St, Toledo, OH", "seller": "Pat", "stage": "Contacted", "nextFollowUp": yday})
put("deals", {"id": "d2", "address": "9 Elm Ave, Cleveland, OH", "seller": "Lee", "stage": "Offer Made", "nextFollowUp": t.isoformat()})
put("deals", {"id": "d3", "address": "1 Done Rd", "stage": "Closed", "nextFollowUp": yday})
put("deals", {"id": "d4", "address": "77 New Lead Ln", "stage": "New Lead"})
put("tasks", {"id": "t1", "title": "Send Pat the contract", "due": yday, "done": False})
put("tasks", {"id": "t2", "title": "Old thing", "due": yday, "done": True})
con.execute("INSERT INTO leads(key,county,state,source_type,address,first_seen,status) VALUES('k1','Lucas','OH','foreclosure','5 Pine St',?,'new')", (nowi,))
con.execute("INSERT INTO leads(key,county,state,source_type,address,first_seen,status) VALUES('k2','Lucas','OH','foreclosure','old',?,'new')", ((datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"),))
con.execute("INSERT INTO messages(phone,direction,body,sid,at) VALUES('+12165550100','in','Yes I might sell','s1',?)", (nowi,))
con.execute("INSERT INTO messages(phone,direction,body,sid,at) VALUES('+12165550100','out','Hi','s2',?)", (nowi,))
con.execute("INSERT INTO hq_email_replies(prospect_id,message_id,from_addr,at,text,class) VALUES('p1','m1','owner@co.com',?,'Lets talk Tuesday','meeting')", (nowi,))
con.execute("INSERT INTO hq_email_replies(prospect_id,message_id,from_addr,at,text,class) VALUES('p2','m2','x@co2.com',?,'Who is this?',NULL)", (nowi,))
con.commit()
mt = hq.create_task(con, "ae.book_meeting", {"attendee": "owner@co.com", "start": f"{t.isoformat()}T14:00", "end": f"{t.isoformat()}T14:30", "title": "AI Sidekicks intro call", "prospect_id": "p1"}, division="demigod")
hq.approve(con, mt["id"], mt["params_hash"]); hq.action_done(con, mt["id"], {"event_id": "ev1"})
hq.create_task(con, "ae.book_meeting", {"attendee": "b@co.com", "start": f"{(t + timedelta(days=1)).isoformat()}T10:00", "end": f"{(t + timedelta(days=1)).isoformat()}T10:30", "title": "Second call", "prospect_id": "p2"}, division="demigod")
db.kv_set(con, "hq_last_tick", nowi); con.commit()

b = hq.morning_brief(con, cfg)
m, d = b["molabucks"], b["demigod"]
ok(m["follow_ups_due"] == 2 and m["follow_ups"][0]["address"].startswith("12 Oak") and m["follow_ups"][0]["overdue"], "seller follow-ups due, overdue first, closed deals left out")
ok(m["new_leads_not_contacted"] == 1 and "New Lead Ln" not in b["headline"], "brand-new leads never contacted are counted apart from follow-ups (not in the headline)")
ok(m["tasks_due_count"] == 1 and m["tasks_due"][0]["title"] == "Send Pat the contract", "open tasks due today or overdue (done ones left out)")
ok(m["new_county_leads"] == 1 and m["county_leads_waiting"] == 2, "new county leads in the last 24 hours, plus how many wait for review")
ok(m["seller_texts"] == 1 and m["seller_texts_sample"][0]["text"] == "Yes I might sell", "seller texts that came in (not his own)")
ok(d["replies"] == 2 and d["wants_call"] == 1 and d["unread"] == 1, "AI Sidekicks replies, who wants a call, and not-read-yet")
ok([c["day"] for c in d["calls"]] == ["today", "tomorrow"] and d["calls"][0]["booked"] and not d["calls"][1]["booked"], "calls today (booked) and tomorrow (still waiting on approval)")
ok(b["approvals_waiting"] == 1 and b["engine"]["loop_running"], "approvals waiting, engine loop running")
ok(b["headline"].startswith("Today: an AI Sidekicks call at 2 PM") and "2 AI Sidekicks replies (1 wants a call)" in b["headline"] and "2 seller follow-ups" in b["headline"] and "1 approval waiting" in b["headline"], b["headline"])
fake = {"molabucks": {"follow_ups_due": 0, "tasks_due_count": 0, "seller_texts": 0, "new_county_leads": 50},
        "demigod": {"calls": [], "replies": 0, "wants_call": 0, "follow_ups_due": 76}, "approvals_waiting": 0}
ok(hq._brief_line(fake) == "Today: 76 AI Sidekicks follow-ups due and 50 new county leads.", "AI Sidekicks follow-ups due are in the headline (" + hq._brief_line(fake) + ")")
ok(hq.handle(con, cfg, {"op": "morning_brief"})["headline"] == b["headline"] and "morning_brief" in hq.READ_OPS and "morning_brief" not in hq.WRITE_OPS, "available as a read-only op (PC link read channel)")
db.kv_set(con, "di_gmail_error", "Gmail sign-in expired. Double-click Connect-Gmail.bat in the JARVIS folder to sign in again, then restart JARVIS."); con.commit()
bx = hq.morning_brief(con, cfg)
ok(bx["problems"] and "sign-in expired" in bx["problems"][0]["title"], "an expired Gmail sign-in shows at the top of the brief's problems")
db.kv_set(con, "di_gmail_error", ""); con.commit()
before = con.total_changes; hq.morning_brief(con, cfg)
ok(con.total_changes == before, "reading the brief changes nothing")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
