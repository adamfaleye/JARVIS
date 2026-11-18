"""Executive morning brief: both businesses, workforce, infrastructure, incidents and history, from the engine's own records.
Run: python tests/test_exec_brief.py"""
import json, sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, hq, exec_brief as xb
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
cfg = {"timezone": "America/New_York"}
con = hq.connect()
now = datetime.now(); iso = lambda d: d.isoformat(timespec="seconds")  # noqa: E731
t0 = datetime.combine(now.date(), datetime.min.time())
yday_noon, today_early = iso(t0 - timedelta(hours=12)), iso(t0 + timedelta(minutes=1))

print("Quiet engine, loop never ran")
b = hq.handle(con, cfg, {"op": "exec_brief"})
ok("exec_brief" in hq.READ_OPS and "exec_brief" not in hq.WRITE_OPS, "available as a read-only op")
ok(b["status"]["overall"] == "Critical" and "work loop not running" in b["status"]["overall_reasons"], "a loop that isn't running is Critical, never Healthy")
ok([w["employee"] for w in b["workforce"]] == [x[1] for x in xb.WORKFORCE] and len(b["workforce"]) == 9, "exactly the 9 AI Sidekicks employees, in order")
ok(all(w["status"] in ("offline", "blocked") for w in b["workforce"]), "nobody shows as working while the loop is down")
ok(not b.get("sections_unavailable"), "every section computes on an empty database")
ok(b["history"]["yesterday"] is None and b["history"]["snapshots_saved"] == 0, "no history yet is reported as missing, not invented")

print("Activity uses calendar days, test sends excluded")
def done_task(kind, agent, finished, test=0, status="completed", division="demigod"):
    tid = f"t{abs(hash((kind, finished, test, status)))}"
    con.execute("INSERT INTO hq_tasks(id,agent,kind,title,status,created,updated,started,finished,test,division,verified) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                (tid, agent, kind, kind, status, finished, finished, finished, finished, test, division))
done_task("sdr.send_email", "sdr", yday_noon)
done_task("sdr.send_email", "sdr", iso(t0 - timedelta(hours=11)))
done_task("sdr.send_email", "sdr", iso(t0 - timedelta(hours=10)), test=1)
done_task("sdr.send_email", "sdr", today_early)
con.execute("INSERT INTO leads(key,county,state,source_type,address,event_date,first_seen,status) VALUES('a','Pinellas','FL','foreclosure','1 A St',?,?,'new')",
            ((now.date() + timedelta(days=3)).isoformat(), yday_noon))
con.execute("INSERT INTO leads(key,county,state,source_type,address,event_date,first_seen,status) VALUES('b','Pinellas','FL','foreclosure','',?,?,'new')",
            ((now.date() + timedelta(days=20)).isoformat(), iso(t0 - timedelta(days=3))))
con.execute("INSERT INTO hq_email_replies(prospect_id,message_id,from_addr,at,text,class) VALUES('p1','m1','a@co.com',?,'yes','interested')", (yday_noon,))
con.commit()
b = xb.exec_brief(con, cfg)
y, td = b["activity"]["yesterday"], b["activity"]["today"]
ok(y["demigod"]["emails_sent"] == 2 and td["demigod"]["emails_sent"] == 1, f"yesterday's sends (2, test excluded) are separate from today's (1): {y['demigod']['emails_sent']}/{td['demigod']['emails_sent']}")
ok(y["molabucks"]["county_leads_new"] == 1 and td["molabucks"]["county_leads_new"] == 0, "a lead found yesterday isn't counted again today (no rolling-window double count)")
ok(y["demigod"]["replies_positive"] == 1, "positive replies counted by class")
ok(b["activity"]["last_7_days"]["demigod"]["emails_sent"] == 2 and b["activity"]["prior_7_days"]["demigod"]["emails_sent"] == 0, "week vs prior week windows")
acq = b["molabucks"]["acquisitions"]
ok(acq["upcoming_sales_21d_count"] == 2 and acq["upcoming_missing_address"] == 1 and acq["upcoming_by_week"]["0-7 days"] == 1, "auction deadlines in the next 21 days, with missing addresses counted")
ok(b["status"]["molabucks"] == "Needs Attention", "a sale within 14 days makes MolaBucks Needs Attention")

print("Incidents: Gmail quota, verified recovery, loop downtime")
db.kv_set(con, "di_gmail_error", "Gmail error 403: Quota exceeded for quota metric 'Total Query Cost'"); con.commit()
xb.watch(con, cfg); xb.watch(con, cfg)
inc = con.execute("SELECT key, count, resolved_at FROM hq_incidents WHERE key='gmail_quota'").fetchone()
ok(inc and inc["count"] == 2 and inc["resolved_at"] is None, "a Gmail 403 quota error opens one incident and counts repeats")
db.kv_set(con, "hq_last_tick", iso(datetime.now())); con.commit()
b = xb.exec_brief(con, cfg)
ok(b["status"]["overall"] == "Needs Attention" and any(i["key"] == "gmail_quota" for i in b["infrastructure"]["incidents_open"]), "loop running + open quota incident = Needs Attention, quota visible")
ok(b["infrastructure"]["email_sender"]["error_kind"] == "gmail_quota", "the sender error is classified as quota exhaustion")
db.kv_set(con, "di_gmail_error", ""); con.commit()
xb.watch(con, cfg)
b = xb.exec_brief(con, cfg)
ok(not b["infrastructure"]["incidents_open"] and b["infrastructure"]["incidents_resolved_7d"][0]["key"] == "gmail_quota", "it's closed only after the loop sees the error gone, and kept in the 7-day history")
db.kv_set(con, "hq_last_tick", iso(datetime.now() - timedelta(hours=3))); con.commit()
xb.on_loop_start(con)
r = con.execute("SELECT * FROM hq_incidents WHERE key='loop_down'").fetchone()
ok(r and r["resolved_at"] and "3 h" in r["message"], f"a heartbeat gap is recorded as loop downtime on restart ({r['message'] if r else None})")
xb.watch(con, cfg, loop_error="RuntimeError: boom")
ok(con.execute("SELECT COUNT(*) FROM hq_incidents WHERE key='loop_error' AND resolved_at IS NULL").fetchone()[0] == 1, "a loop crash opens an incident")
xb.watch(con, cfg)
ok(con.execute("SELECT COUNT(*) FROM hq_incidents WHERE key='loop_error' AND resolved_at IS NULL").fetchone()[0] == 0, "a clean tick resolves it")
con.execute("INSERT INTO runs(kind,target,started,finished,found,new,error) VALUES('scrape','Pinellas FL (foreclosure)',?,?,0,0,'Timeout')", (iso(now), iso(now))); con.commit()
xb.watch(con, cfg)
ok(con.execute("SELECT COUNT(*) FROM hq_incidents WHERE key LIKE 'scrape:%' AND resolved_at IS NULL").fetchone()[0] == 1, "a failed county scrape opens an incident")

print("Stale vs live blockers, stuck tasks")
done_task("sdr.campaign_email", "sdr", iso(now - timedelta(days=5)), status="blocked")
for i in range(3):
    done_task("sdr.campaign_email", "sdr", iso(now - timedelta(minutes=5 + i)), status="blocked")
db.kv_set(con, "hq_last_tick", iso(datetime.now())); con.commit()
b = xb.exec_brief(con, cfg)
bw = b["demigod"]["outreach"]["blocked_work"]
ok(bw and bw[0]["n"] == 3 and b["demigod"]["outreach"]["stale_blocked"]["count"] == 1, "recent blocked drafts are live work; old ones are reported as history")
sdr = next(w for w in b["workforce"] if w["id"] == "sdr")
ok(sdr["blocker"] is not None and sdr["failed_or_blocked_24h"] == 3, "the SDR's live blocker and 24 h failures show")
xb.watch(con, cfg)
ok(con.execute("SELECT COUNT(*) FROM hq_incidents WHERE key='repeat_fail:sdr.campaign_email' AND resolved_at IS NULL").fetchone()[0] == 1, "3+ failures of one task type in 24 h open a repeated-failure incident")
con.execute("INSERT INTO hq_tasks(id,agent,kind,title,status,created,updated,lease_until,division) VALUES('stuck1','sdr','sdr.find_contact','x','running',?,?,?,'demigod')",
            (iso(now), iso(now), iso(now - timedelta(minutes=30)))); con.commit()
b = xb.exec_brief(con, cfg)
ok(len(b["infrastructure"]["stuck_tasks"]) == 1, "a running task past its lease is reported as stuck")

print("Snapshots and history")
ok(xb.snapshot(con) is True and xb.snapshot(con) is False, "one snapshot per day")
con.execute("INSERT INTO hq_daily_snapshot(day,at,data) VALUES(?,?,?)", ((now.date() - timedelta(days=1)).isoformat(), yday_noon, json.dumps({"county_leads_waiting": 7})))
con.commit()
b = xb.exec_brief(con, cfg)
ok(b["history"]["yesterday"]["levels"]["county_leads_waiting"] == 7 and b["history"]["week_ago"] is None, "yesterday's saved levels are compared; missing days stay missing")

print("Resilience and read-only")
orig = xb.levels
xb.levels = lambda c: (_ for _ in ()).throw(RuntimeError("broken"))
b = xb.exec_brief(con, cfg)
xb.levels = orig
ok(b["history"].get("unavailable") and "history" in b["sections_unavailable"] and b["demigod"].get("campaigns") is not None, "one broken section doesn't take down the others")
ok(any("unavailable" in r for r in b["status"]["overall_reasons"]), "an unavailable section is named in the status reasons")
before = con.total_changes; xb.exec_brief(con, cfg)
ok(con.total_changes == before, "reading the brief changes nothing")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
