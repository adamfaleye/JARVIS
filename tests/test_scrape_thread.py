"""The daily county scrape must not hold up sending: it runs in its own thread.
Run: python tests/test_scrape_thread.py"""
import sys, tempfile, threading, time
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
import run
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

release = threading.Event(); calls = {"run": 0, "fail": False}
def fake_run(cfg, *a, **k):
    calls["run"] += 1
    if calls["fail"]:
        raise RuntimeError("site down")
    release.wait(5)
run.scrape.run = fake_run; run.scrape.export = lambda *a, **k: None
con = db.connect(); state = {}
morning = datetime.now().replace(hour=10)

t0 = time.time(); t = run.maybe_start_scrape({}, con, morning, state, 6)
ok(t is not None and time.time() - t0 < 1, "the scrape starts in the background and the loop moves on right away")
time.sleep(0.3)
ok(run.maybe_start_scrape({}, con, morning, state, 6) is t and calls["run"] == 1, "only one scrape at a time")
release.set(); t.join(5)
ok(db.kv_get(con, "last_scrape_day") == morning.strftime("%Y-%m-%d"), "a finished scrape marks the day done")
ok(run.maybe_start_scrape({}, con, morning, state, 6) is None and calls["run"] == 1, "no second scrape the same day")

db.kv_set(con, "last_scrape_day", "2000-01-01"); con.commit()
ok(run.maybe_start_scrape({}, con, morning.replace(hour=5), {}, 6) is None, "no scrape before the scheduled hour")
calls["fail"] = True; s2 = {}
run.maybe_start_scrape({}, con, morning, s2, 6).join(5)
ok(db.kv_get(con, "last_scrape_day") == "2000-01-01" and s2.get("failed_at"), "a failed scrape doesn't mark the day done")
ok(run.maybe_start_scrape({}, con, morning, s2, 6) is None and calls["run"] == 2, "a failed scrape waits 30 minutes before trying again")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
