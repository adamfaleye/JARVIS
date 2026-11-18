"""'Hey Jarvis, log a call with the Doe estate, interested, call back Friday.' Run: python tests/test_call_by_name.py"""
import json, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, crm
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

con = db.connect()
soon = (date.today() + timedelta(days=7)).isoformat()
db.insert_lead(con, {"county": "Pinellas", "state": "FL", "source_type": "tax_deed", "case_number": "T1", "address": "100 SAMPLE TER N", "city": "PINELLAS PARK",
                     "zip": "33782", "owner": "DOE, JANE EST", "event_date": soon, "judgment_amount": 14766.73})
db.insert_lead(con, {"county": "Pinellas", "state": "FL", "source_type": "tax_deed", "case_number": "T2", "address": "200 EXAMPLE ST N", "city": "PINELLAS PARK",
                     "owner": "ROE, KAREN R WILCOX / WILCOX, MICHAEL W", "event_date": soon})
db.insert_lead(con, {"county": "Pinellas", "state": "FL", "source_type": "tax_deed", "case_number": "T3", "address": "100 WILCOX AVE", "city": "LARGO", "owner": "WILCOX, JANE", "event_date": soon})
con.commit()
ctx = lambda *said: {"user_texts": list(said), "request": said[0]}  # noqa: E731

print("A county lead becomes a deal with the call logged")
fri = date.today() + timedelta(days=(4 - date.today().weekday()) % 7 or 7)
r = crm.log_call_by_name(con, "the Doe estate", "talked", note="interested", followUpDate=fri.isoformat(), dates_said={"followUp": "Friday"},
                         _ctx=ctx("log a call with the Doe estate, interested, call back Friday"))
ok(r["status"] == "ok" and "Added 100 SAMPLE TER N" in r["summary"] and "Logged talked" in r["summary"], r.get("summary") or r)
d = crm.search_deals(con, "100 SAMPLE")["deals"][0]
full = crm.get(con, "deals", d["id"])
ok(full["seller"] == "DOE, JANE EST" and full["county"] == "Pinellas" and full["stage"] == "Contacted" and full["nextFollowUp"] == fri.isoformat(),
   f"the owner is the seller, it's marked contacted, and the follow-up is Friday ({full['nextFollowUp']})")
ok("Tax deed sale" in full["notes"] and "14,767" in full["notes"] and "interested" in full["notes"], "the sale date, amount owed and what they said are in the notes")
ok(con.execute("SELECT status FROM leads WHERE case_number='T1'").fetchone()[0] == "synced", "the county lead leaves the review list")

print("The next call finds the deal")
r = crm.log_call_by_name(con, "Doe", "no_answer", _ctx=ctx("no answer at Doe"))
ok(r["status"] == "ok" and "Logged no answer on 100 SAMPLE TER N" in r["summary"] and len(crm.items(con, "deals")) == 1, "found by name, no duplicate deal")

print("Asks instead of guessing")
r = crm.log_call_by_name(con, "Wilcox", "talked", _ctx=ctx("talked to Wilcox"))
ok(r["status"] == "needs_input" and "200 EXAMPLE ST N" in r["question"] and "100 WILCOX AVE" in r["question"], "two county leads match: asks which")
r = crm.log_call_by_name(con, "Johnson", "talked", _ctx=ctx("talked to Johnson"))
ok(r["status"] == "needs_input" and "address" in r["question"], "nobody matches: asks for the address")
r = crm.log_call_by_name(con, "Doe", "talked", followUpDate="2026-12-01", dates_said={"followUp": "next month sometime"}, _ctx=ctx("Doe, call back Tuesday"))
ok(r["status"] == "needs_input", "a follow-up date he didn't actually say is questioned, not saved")
r = crm.log_call_by_name(con, "Doe", "dead", _ctx=ctx("Doe is dead"))
ok(r["status"] == "needs_input" and "reason" in r["question"], "dead needs a reason")

src = (ROOT / "voice" / "jarvis_voice.py").read_text(encoding="utf-8")
ok('("log_call_by_name"' in src and '"log_call_by_name"' in src.split("WRITES =")[1][:200] and "use log_call_by_name" in src, "available by voice, with Mola's own words checked")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
