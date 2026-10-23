"""HQ task-engine tests. Run: python -m tests.test_hq  (throwaway database; no outside calls)."""
import json, os, sys, tempfile
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, crm, hq

tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"
config.ROOT = tmp
(tmp / "data").mkdir()
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
    os.environ.pop(k, None)
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
def raises(fn, frag):
    try:
        fn()
    except ValueError as e:
        return frag.lower() in str(e).lower()
    return False

cfg = {"timezone": "America/New_York", "sms": {"enabled": False, "require_consent": True, "quiet_hours": {"start": "09:00", "end": "20:00"},
       "daily_limit": 150, "dnc_file": "data/dnc.txt", "sender_name": "Ademola", "sequence": []}, "counties": []}
con = hq.connect()
quiet = lambda *a: None
# CRM fixtures (the shapes the JARVIS app syncs)
today = date.today()
crm.put(con, "settings", {"id": "settings", "goal": 50000, "defaultFee": 12000})
crm.put(con, "deals", {"id": "d1", "address": "1250 Cheney Hwy Unit C, Titusville, FL 32780", "county": "Brevard", "stage": "Under Contract",
                       "price": 99000, "fee": 10000, "createdAt": "2026-10-01"})
crm.put(con, "deals", {"id": "d2", "address": "55 Oak Ave, Clearwater, FL", "county": "Pinellas", "stage": "Closed", "actualFee": 8000,
                       "closedAt": today.isoformat()})
crm.put(con, "deals", {"id": "d3", "address": "9 Elm St, Tampa, FL", "county": "Hillsborough", "stage": "New Lead", "phone": "8135550101"})
crm.put(con, "buyers", {"id": "b1", "name": "Gulf Coast Holdings", "markets": ["Brevard"], "maxPrice": 150000, "verified": True})
crm.put(con, "contracts", {"id": "c1", "type": "purchase", "status": "Signed", "signedAt": (today - timedelta(days=1)).isoformat(),
                           "fields": {"address": "1250 Cheney Hwy Unit C", "emd": 1000, "emdDays": 3, "closingDays": 10}})
crm.put(con, "offers", {"id": "o1", "dealId": "d1", "status": "pending_approval"})
con.commit()

print("task lifecycle")
ok(raises(lambda: hq.create_task(con, "nope.kind"), "unknown"), "unknown task type rejected")
t = hq.create_task(con, "finance.check", objective="test")
ok(t["status"] == "queued" and t["agent"] == "finance" and t["id"].startswith("t_"), "task has id, owner, status")
d1 = hq.create_task(con, "followup.due_list", dedupe_key="fu:1")
d2 = hq.create_task(con, "followup.due_list", dedupe_key="fu:1")
ok(d1["id"] == d2["id"], "duplicate task prevented by dedupe key")
ran = hq.run_due(con, cfg, log=quiet)
ft = hq.get_task(con, t["id"])
ok(ft["status"] == "completed" and ft["verified"] == 1 and [e["type"] for e in ft["events"]][:3] == ["created", "started", "completed"], "finance task ran with an event trail")
r = ft["result"]
ok(r["realized"] == 8000 and r["projected_signed"] == 10000 and r["offers_pending_approval"] == 1, "finance separates realized from projected")
ok(any(x["kind"] == "EMD due" for x in r["deadlines"]) and any(x["kind"] == "Closing" for x in r["deadlines"]), "EMD and closing deadlines computed from the contract")
ok(r.get("twilio") == "not configured" and "twilio_balance" not in r, "no Twilio balance invented when Twilio isn't set up")
due = hq.get_task(con, d1["id"])
ok(due["status"] == "completed" and "due for a call" in due["summary"], "follow-up list built from the CRM queue")

print("one task per agent, leases and recovery")
a = hq.create_task(con, "hunter.review_backlog")
b = hq.create_task(con, "hunter.top_leads", {"tiers": ["A"]})
con.execute("UPDATE hq_tasks SET status='running', lease_owner='dead', lease_until=?, attempts=1 WHERE id=?", ((datetime.now() - timedelta(minutes=1)).isoformat(timespec='seconds'), a["id"]))
con.commit()
ok(hq.recover(con) == 1 and hq.get_task(con, a["id"])["status"] == "queued", "expired lease is recovered and re-queued")
con.execute("UPDATE hq_tasks SET status='running', lease_owner='other', lease_until=? WHERE id=?", ((datetime.now() + timedelta(minutes=5)).isoformat(timespec='seconds'), a["id"]))
con.commit()
hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, b["id"])["status"] == "queued", "agent busy elsewhere: its next task waits (no double work)")
con.execute("UPDATE hq_tasks SET status='queued', lease_owner=NULL, lease_until=NULL WHERE id=?", (a["id"],)); con.commit()
hq.run_due(con, cfg, log=quiet); hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, a["id"])["status"] == "completed" and hq.get_task(con, b["id"])["status"] == "completed", "both Hunter tasks complete in turn")

print("retries, failure, blockers")
orig = hq.EXECUTORS["closer.pipeline_review"]
calls = {"n": 0}
def boom(con_, cfg_, t_):
    calls["n"] += 1
    raise RuntimeError("database hiccup")
hq.EXECUTORS["closer.pipeline_review"] = boom
c = hq.create_task(con, "closer.pipeline_review")
hq.run_due(con, cfg, log=quiet)
cr = hq.get_task(con, c["id"])
ok(cr["status"] == "queued" and cr["next_run_at"] > hq.now() and "retrying" in cr["events"][-1]["message"], "failure retried with backoff")
far = (datetime.now() + timedelta(hours=3)).isoformat(timespec="seconds")
hq.run_due(con, cfg, at=far, log=quiet); hq.run_due(con, cfg, at=(datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds"), log=quiet)
cr = hq.get_task(con, c["id"])
ok(cr["status"] == "failed" and calls["n"] == 3 and cr["recovery"], "gives up after max attempts with recovery guidance")
dep = hq.create_task(con, "dispo.match_buyers", depends_on=[c["id"]])
hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, dep["id"])["status"] == "blocked", "dependent task blocked when its dependency failed")
hq.EXECUTORS["closer.pipeline_review"] = orig
hq.retry(con, c["id"]); hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, c["id"])["status"] == "completed", "retry after fixing the cause succeeds")
ok(raises(lambda: hq.retry(con, c["id"]), "only failed"), "can't retry a completed task")

print("approvals (policy level 3)")
lt = hq.create_task(con, "followup.launch_campaign", {"campaign_id": "cmp_x"})
ok(lt["status"] == "awaiting_approval" and lt["level"] == 3, "outside commitment waits for approval")
hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, lt["id"])["status"] == "awaiting_approval", "worker never runs it unapproved")
ok(raises(lambda: hq.approve(con, lt["id"], "wronghash"), "changed"), "approval bound to the exact parameters reviewed")
con.execute("UPDATE hq_tasks SET params=?, params_hash=? WHERE id=?", (json.dumps({"campaign_id": "cmp_other"}), hq._h({"campaign_id": "cmp_other"}), lt["id"])); con.commit()
ok(raises(lambda: hq.approve(con, lt["id"], lt["params_hash"]), "changed"), "edited after review → old approval no longer valid")
fresh = hq.get_task(con, lt["id"], events=False)
hq.approve(con, lt["id"], fresh["params_hash"])
con.execute("UPDATE hq_tasks SET params=?, params_hash=? WHERE id=?", (json.dumps({"campaign_id": "cmp_evil"}), hq._h({"campaign_id": "cmp_evil"}), lt["id"])); con.commit()
hq.run_due(con, cfg, log=quiet)
ok(hq.get_task(con, lt["id"])["status"] == "awaiting_approval", "parameters changed after approval → back to the approval tray, not run")
rj = hq.reject(con, lt["id"], "not now")
ok(rj["status"] == "rejected", "owner can reject")

print("goal → plan → assignments")
g = hq.set_goal(con, 100000)
hq.run_due(con, cfg, log=quiet)
pt = hq.get_task(con, g["task"]["id"])
plan = pt["result"]
ok(pt["status"] == "completed" and plan["target"] == 100000 and plan["realized"] == 8000 and plan["remaining"] == 92000, "plan starts from realized fees only")
ok(plan["deals_needed"] >= 1 and plan["contracts_needed"] >= plan["deals_needed"] and plan["offers_needed"] >= plan["contracts_needed"]
   and plan["conversations_needed"] >= plan["offers_needed"], "plan works the funnel backward")
ok("not enough history" in plan["assumptions"][0] and "not a promise" in plan["disclaimer"], "assumptions and limits stated")
kids = hq.list_tasks(con, goal_id=g["goal_id"])
ok(len([k for k in kids if k["source"] == "jarvis"]) == 8 and {k["agent"] for k in kids} >= {"hunter", "followup", "frontdesk", "dispo", "closer", "finance"}, "JARVIS assigned work to all six agents")
g2 = hq.set_goal(con, 120000)
ok(con.execute("SELECT status FROM hq_goals WHERE id=?", (g["goal_id"],)).fetchone()[0] == "replaced", "new goal replaces the old one")
for _ in range(6):
    hq.run_due(con, cfg, log=quiet)
dm = next(t for t in hq.list_tasks(con, agent="dispo") if t["kind"] == "dispo.match_buyers" and t["status"] == "completed")
dmr = hq.get_task(con, dm["id"])["result"]
ok(dmr["deals"][0]["matches"] == 1 and dmr["deals"][0]["top"][0]["name"] == "Gulf Coast Holdings", "Dispositions matched the Brevard deal to the Brevard buyer")

print("schedule and briefing")
tz = ZoneInfo("America/New_York")
mon = datetime(2026, 10, 19, 8, 5, tzinfo=tz)
ids = hq.schedule_tick(con, cfg, mon)
ids2 = hq.schedule_tick(con, cfg, mon.replace(minute=30))
ok(len(ids) == 1 and ids == ids2, "morning briefing scheduled once per day (durable, no duplicates)")
ok(hq.schedule_tick(con, cfg, datetime(2026, 10, 18, 9, 0, tzinfo=tz)) == [], "no weekend briefing by default")
for _ in range(12):
    hq.run_due(con, cfg, log=quiet)
rep = [r for r in hq.reports(con) if r["kind"] == "briefing"]
ok(rep and rep[0]["body"]["lines"] and rep[0]["body"]["approvals_waiting"] == 0, "briefing written from finished tasks")
eod = hq.schedule_tick(con, cfg, mon.replace(hour=18, minute=1))
hq.run_due(con, cfg, log=quiet)
ok(any(r["kind"] == "eod" for r in hq.reports(con)), "end-of-day review written")

print("status projection")
st = hq.agent_status(con)
ok(all(a["state"] in ("idle", "working", "queued", "waiting", "awaiting_approval", "failed", "blocked") for a in st.values()), "every agent has a truthful state")
ok(st["closer"]["scorecard"]["completed_7d"] >= 1 and st["closer"]["running"] is None, "idle agents aren't shown as working; scorecards count verified work")
ok(st["hunter"]["state"] != "working", "no fake activity")

print("bridge")
import yaml, mcp_server
(tmp / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
(tmp / "counties.yaml").write_text("counties: []\n", encoding="utf-8")
ok("isn't allowed" in mcp_server._hunter({"op": "hq.approve", "id": "x"}, write=False)["error"], "approvals can't go through the read channel")
s = mcp_server._hunter({"op": "hq.state"}, write=False)
ok(s.get("ok") and len(s["data"]["agents"]) == 7, "HQ state readable through the PC link")
ct = mcp_server._hunter({"op": "hq.create_task", "kind": "jarvis.plan"}, write=True)
ok("itself" in ct.get("error", ""), "owner can't forge JARVIS-internal tasks")

print("pause, away report, health")
mark = hq.now()
pt = hq.create_task(con, "followup.due_list", objective="pause test")
hq.set_paused(con, True, reason="test")
ok(hq.paused(con) and hq.run_due(con, cfg, log=quiet) == [] and hq.get_task(con, pt["id"])["status"] == "queued", "paused: the worker starts nothing")
ok(hq.schedule_tick(con, cfg) == [], "paused: no scheduled work is created")
ok(raises(lambda: hq.handle(con, cfg, {"op": "run_now"}), "paused"), "paused: run-now is refused")
ok(hq.health(con)["paused"] and hq.health(con)["queued"] >= 1, "health reports the pause and the queue")
hq.set_paused(con, False)
crm.put(con, "settings", {"id": "settings", "goal": 50000, "defaultFee": 12000, "emergencyStop": True}); con.commit()
ok("Emergency stop" in hq.paused(con) and hq.run_due(con, cfg, log=quiet) == [], "the CRM emergency stop also halts the agents")
crm.put(con, "settings", {"id": "settings", "goal": 50000, "defaultFee": 12000}); con.commit()
hq.run_due(con, cfg, log=quiet)
aw = hq.since(con, mark)
ok(any(x["id"] == pt["id"] for x in aw["completed"]) and all(x["id"] != pt["id"] for x in aw["problems"]), "away report lists verified completions since you left")
ok("still_waiting" in aw and "health" in aw and aw["attempts"] >= 1, "away report includes approvals waiting and worker health")
ok(mcp_server._hunter({"op": "hq.pause"}, write=False).get("error") and mcp_server._hunter({"op": "hq.since", "ts": mark}, write=False).get("ok"), "pause needs the write channel; the away report is read-only")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
