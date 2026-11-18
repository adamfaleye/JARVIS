"""Past auctions leave the county-lead review list; postponed sales come back. Run: python tests/test_lead_expiry.py"""
import sys, tempfile
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

con = db.connect()
t = date.today()
d = lambda n: (t + timedelta(days=n)).isoformat()  # noqa: E731
rec = lambda case, when: {"county": "Pinellas", "state": "FL", "source_type": "foreclosure_auction", "case_number": case, "address": f"{case} Main St", "event_date": when}  # noqa: E731
for case, when in [("A", d(-30)), ("B", d(-1)), ("C", d(0)), ("D", d(7)), ("E", "")]:
    db.insert_lead(con, rec(case, when))
con.execute("UPDATE leads SET status='synced' WHERE case_number='A'"); con.commit()
waiting = lambda: [r[0] for r in con.execute("SELECT case_number FROM leads WHERE status IN ('new','exported') AND " + db.UPCOMING + " ORDER BY case_number")]  # noqa: E731
ok(waiting() == ["C", "D", "E"], f"lists only sales today or later, plus leads with no date ({waiting()})")
n = db.expire_past(con)
ok(n == 1 and con.execute("SELECT status FROM leads WHERE case_number='B'").fetchone()[0] == "expired", "a lead still waiting whose sale already happened is set aside")
ok(con.execute("SELECT status FROM leads WHERE case_number='A'").fetchone()[0] == "synced", "leads already added to JARVIS are never touched")
ok(db.insert_lead(con, rec("B", d(14))) is False and con.execute("SELECT status, event_date FROM leads WHERE case_number='B'").fetchone()[:] == ("new", d(14)),
   "a sale the county postponed comes back to the review list with its new date")
ok(db.insert_lead(con, rec("D", d(3))) is False and con.execute("SELECT event_date FROM leads WHERE case_number='D'").fetchone()[0] == d(7), "an older date never overwrites a newer one")
src = (ROOT / "run.py").read_text(encoding="utf-8")
ok("db.expire_past(con)" in src and "leads_expired_day" in src, "the engine loop sets past auctions aside once a day")
for f, needle in [("mcp_server.py", "db.UPCOMING"), ("engine/hq.py", "status='new' AND \" + db.UPCOMING"), ("voice/jarvis_voice.py", "db.UPCOMING"), ("engine/scrape.py", "db.UPCOMING")]:
    ok(needle in (ROOT / f).read_text(encoding="utf-8"), f"{f} only shows upcoming sales")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
