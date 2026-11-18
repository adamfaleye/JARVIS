"""Stage 2: standing duties. Agents get work on a schedule without being asked, never twice, resume after a restart,
stay inside per-agent budgets, report honest states, and the database is backed up and restore-tested daily.
No network: the AI is faked. Run: python tests/test_duties.py"""
import json, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, agents_ai, duties
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
quiet = lambda *a, **k: None  # noqa: E731

class FakeAI:
    def __init__(s): s.calls = []; s.messages = s
    def create(s, **kw):
        s.calls.append(kw)
        txt = json.dumps(kw.get("messages"))
        if "Use web search to find" in txt:
            n = len([c for c in s.calls if "Use web search to find" in json.dumps(c.get("messages"))])
            body = {"summary": "found", "deliverable": "Found 2 businesses with public pages in the area; one lists a contact email on its own website, the other only a form.", "needs_info": [],
                    "prospects": [{"company": f"Acme Heating {n}", "website": f"acmeheat{n}.com", "city": "Quincy", "state": "MA", "phone": "", "email": f"info@acmeheat{n}.com", "units": "", "notes": "HVAC", "source": f"https://acmeheat{n}.com"},
                                  {"company": f"Bolt Plumbing {n}", "website": f"boltplumb{n}.com", "city": "Medford", "state": "MA", "phone": "", "email": "", "units": "", "notes": "plumbing", "source": f"https://boltplumb{n}.com"}]}
        elif "published contact email" in txt:
            body = {"summary": "found", "deliverable": "", "needs_info": [], "email": "office@boltplumb1.com", "email_source": "https://boltplumb1.com/contact", "contact_page": "", "phone": "", "website": "boltplumb1.com"}
        elif "Follow-up" in txt or "follow-up email" in txt:
            body = {"summary": "ok", "deliverable": "Short follow-up drafted with one new reason to talk and an opt-out line.", "needs_info": [], "drafts": [{"channel": "email", "subject": "Re: hi", "body": "Hi again..."}]}
        else:
            body = {"summary": "ok", "deliverable": "x" * 120, "needs_info": [], "drafts": [{"format": "LinkedIn post", "body": "..."}],
                    "facts": [{"fact": "Family-owned plumber", "source": "https://boltplumb1.com/about"}]}
        return NS(content=[NS(type="text", text=json.dumps(body), citations=[])], stop_reason="end_turn",
                  usage=NS(input_tokens=4000, output_tokens=600, server_tool_use=NS(web_search_requests=1)))
fake = FakeAI(); agents_ai.CLIENT = fake
cfg = {"timezone": "America/New_York", "hq": {"weekends": True}}
con = hq.connect()
db.kv_set(con, "hq_last_tick", datetime.now().isoformat(timespec="seconds"))

print("Standing duties run on schedule, once")
day = datetime(2026, 11, 16, 9, 31)  # a Monday
made = duties.tick(con, cfg, day, log=quiet)
ok({"duty_backup_day", "duty_qa_day", "duty_discover_day", "duty_mkt_week"} <= set(made), f"by 9:31 Monday: backup, QA check, discovery and marketing plan ({made})")
again = duties.tick(con, cfg, day + timedelta(minutes=1), log=quiet)
ok(not ({"duty_qa_day", "duty_discover_day", "duty_mkt_week"} & set(again)), "the next tick (30 s later in real life) starts nothing twice")
s = hq.searches(con)["searches"][0]
ok(s["source"] == "duty" and s["industry"] == "home services" and s["count"] == 25, f"SDR discovery: 25 home-services businesses, first in the rotation ({s['industry']}, {s['area']})")
ft = hq.get_task(con, s["find_tasks"][0], events=False)
ok("HVAC, plumbing and roofing" in ft["params"]["industry"] and ft["division"] == "demigod", "the search names the industry, not property management")
hq.run_due(con, cfg, max_tasks=20, log=quiet)
prompt = json.dumps([c for c in fake.calls if "Use web search to find" in json.dumps(c.get("messages"))][0]["messages"])
ok("home service contractors" in prompt and "property management companies (and" not in prompt, "the SDR's web search asks for that industry")
s = hq.search_get(con, s["id"])
ok(s["done"] and s["found"] >= 2 and s["with_email"] >= 1, f"discovery finished: {s['found']} found, {s['with_email']} with a published email")
duties.tick(con, cfg, day + timedelta(minutes=40), log=quiet)
s = hq.search_get(con, s["id"])
ok(s["lookups"] >= 1, "30 minutes later the SDR looks up emails for the ones without (published only, never guessed)")
qa = [t for t in hq.list_tasks(con, limit=200) if t["kind"] == "qa.system_check"]
ok(qa and qa[0]["status"] == "completed" and "problem" in (qa[0].get("summary") or "").lower() or "normal" in (qa[0].get("summary") or "").lower(),
   f"QA system check ran and reported ({(qa[0].get('summary') or '')[:90]})")
mk = [t for t in hq.list_tasks(con, limit=200) if t["kind"] == "marketing.content_draft"]
ok(mk and "comment a word" in json.dumps(mk[0]["params"]).lower() and "Higgsfield" in json.dumps(mk[0]["params"]), "Marketing drafts next week's plan: comment-a-word CTAs, real features only, no Higgsfield")
day2 = day + timedelta(days=1)
duties.tick(con, cfg, day2.replace(hour=9, minute=5), log=quiet)
inds = sorted(x["industry"] for x in hq.searches(con)["searches"])
ok(inds == ["home services", "property management"], f"day 2 rotates to the next industry ({inds})")
ok(not [t for t in hq.list_tasks(con, limit=300) if t["kind"] == "marketing.content_draft" and t["id"] != mk[0]["id"]], "the marketing plan is weekly, not daily")

print("Follow-ups and the PM's blocker report")
e1 = hq.create_task(con, "sdr.send_email", {"to": "dana@bay.com", "subject": "Hi Bay", "body": "Hi Dana ... reply no thanks and I won't follow up.", "prospect_id": "dp_bay", "company": "Bay Rentals"}, division="demigod")
hq.approve(con, e1["id"], e1["params_hash"]); hq.email_sent(con, e1["id"], "m_bay1", "th_bay")
con.execute("UPDATE hq_tasks SET result=? WHERE id=?", (json.dumps({"message_id": "m_bay1", "thread_id": "th_bay", "sent_at": (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")}), e1["id"])); con.commit()
last_at = datetime.now().replace(hour=10, minute=5) + timedelta(days=3)
duties.tick(con, cfg, last_at, log=quiet)
fus = [t for t in hq.list_tasks(con, limit=300) if t["kind"] == "sdr.followup_draft"]
ok(len(fus) == 1 and fus[0]["params"]["step"] == 2 and fus[0]["source"] == "duty", "a follow-up #2 is drafted for the prospect who didn't reply (sending still needs approval)")
duties.followups(con, cfg, datetime.now(), log=quiet)
ok(len([t for t in hq.list_tasks(con, limit=300) if t["kind"] == "sdr.followup_draft"]) == 1, "never drafted twice")
hq.create_task(con, "pm.daily_status", {"day": "x"}, division="demigod", source="duty")
hq.run_due(con, cfg, max_tasks=20, log=quiet)
pm = [t for t in hq.list_tasks(con, limit=300) if t["kind"] == "pm.daily_status"][0]
ok(pm["status"] == "completed" and "done in 24 h" in pm["summary"], f"PM blocker report ({pm['summary']})")

print("Budgets: one agent can't use the whole day")
duties.set_budgets(con, {"sdr": 0.001})
before = len(fake.calls)
t = hq.create_task(con, "sdr.research_prospect", {"company": "Acme Heating"}, division="demigod")
hq.run_due(con, cfg, max_tasks=5, log=quiet)
t = hq.get_task(con, t["id"], events=False)
ok(len(fake.calls) == before and t["status"] == "waiting" and "budget" in (t.get("progress_note") or t.get("note") or json.dumps(t)).lower(),
   "over its daily budget the SDR's task waits for tomorrow; no AI call is made")
ok(duties.discover(con, cfg, datetime.now(), log=quiet) is None, "and no new discovery starts")
duties.set_budgets(con, {"sdr": 2.5})
try:
    duties.set_budgets(con, {"sdr": 500}); bad = False
except ValueError:
    bad = True
ok(bad, "budgets are capped at $50/day per agent")

print("Honest states")
st = hq.agent_status(con)
ok(st["support"]["state"] in ("idle_monitoring",) and "support requests" in st["support"]["monitoring"], "Support with nothing to do = idle, monitoring (and says what)")
ok(st["sdr"]["duties"] and st["sdr"]["budget"] == 2.5, "the board shows each agent's standing duties and budget")
db.kv_set(con, "hq_last_tick", (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds"))
full = hq.state(con, cfg)
ok(not full["engine_online"] and all(a["state"] == "offline" for a in full["agents"].values() if a.get("operational")), "engine silent for 20 min: every agent shows offline")
db.kv_set(con, "hq_last_tick", datetime.now().isoformat(timespec="seconds"))

print("Restart safety")
running = hq.create_task(con, "sdr.research_prospect", {"company": "Bolt Plumbing"}, division="demigod")
con.execute("UPDATE hq_tasks SET status='running', lease_owner='dead', lease_until=?, attempts=1 WHERE id=?", ((datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"), running["id"])); con.commit()
con.close()
con = hq.connect()  # the engine restarts
hq.run_due(con, cfg, max_tasks=10, log=quiet)
ok(hq.get_task(con, running["id"], events=False)["status"] == "completed", "a task cut off by a restart is picked up and finished")
n = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE source='duty'").fetchone()[0]
duties.tick(con, cfg, last_at + timedelta(minutes=2), log=quiet)
ok(con.execute("SELECT COUNT(*) FROM hq_tasks WHERE source='duty'").fetchone()[0] == n, "after a restart, today's duties aren't created again")

print("Backups")
b = json.loads(db.kv_get(con, "backup_last"))
ok(b["ok"] and b["integrity"] == "ok" and Path(b["path"]).exists() and "hq_tasks" in b["counts"], "daily backup made and restore-tested (opens, integrity ok, same rows)")
for i in range(16):
    duties.backup_daily(con, log=quiet)
ok(len(list(Path(b["path"]).parent.glob("*daily*"))) <= duties.BACKUPS_KEPT, f"only the last {duties.BACKUPS_KEPT} daily backups are kept")
brief = hq.morning_brief(con, cfg)
ok("system_check" in brief and brief["system_check"] and "problems" in brief["system_check"], "the morning brief carries the QA system check")
ok({"duties", } <= hq.READ_OPS and "set_ai_budgets" in hq.WRITE_OPS, "the app can read duties and set budgets")
agents_ai.CLIENT = None
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
