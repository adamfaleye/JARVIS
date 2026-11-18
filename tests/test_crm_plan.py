"""CRM plans (Solutions Architect) and the CRM line in AI Sidekicks' pitch. Run: python tests/test_crm_plan.py"""
import json, sys, tempfile
from types import SimpleNamespace as NS
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, hq, agents_ai
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
def raises(fn, frag):
    try: fn()
    except ValueError as e: return frag.lower() in str(e).lower()
    return False
con = hq.connect(); cfg = {"timezone": "America/New_York"}
P = {"recordKind": "prospect", "recordId": "p1", "company": "Bass River Rentals", "city": "Beverly", "state": "MA"}
print("Gate")
ok(raises(lambda: hq.create_task(con, "architect.crm_plan", P, division="demigod"), "only made for companies that said yes"), "no CRM plan for a prospect who hasn't said yes")
con.execute("INSERT INTO hq_email_replies(prospect_id,message_id,from_addr,at,text,class) VALUES('p1','m1','o@b.com','2026-11-09T09:00','Sure, lets talk','interested')"); con.commit()
t = hq.create_task(con, "architect.crm_plan", P, division="demigod")
ok(t["status"] == "queued" and t["agent"] == "architect" and t["level"] == 2, "after a yes, the Solutions Architect gets it (you review the plan)")
ok(hq.create_task(con, "architect.crm_plan", {"recordKind": "project", "recordId": "pr1", "company": "Client Co"}, division="demigod")["status"] == "queued", "clients' projects can get a CRM plan any time")
ok(raises(lambda: hq.create_task(con, "architect.crm_plan", P, division="molabucks"), ""), "it's AI Sidekicks work only")
print("Plan output")
plan = {"summary": "CRM plan for Bass River", "deliverable": "A CRM for owners, units, tenants and work orders, in two phases." * 2, "needs_info": [],
        "records": [{"name": "Unit", "fields": ["address", "beds", "rent"]}], "stages": [{"pipeline": "Leasing", "steps": ["Inquiry", "Tour", "Application", "Lease signed"]}],
        "automations": [{"when": "Lease ends in 60 days", "then": "Remind the manager to start the renewal"}], "integrations": [{"system": "Gmail", "purpose": "log emails", "known": False}],
        "screens": ["Units board"], "phases": [{"name": "Phase 1", "includes": ["Units", "Tenants"]}], "open_questions": ["Which software do you use for rent today?"]}
seen = {}
class Fake:
    def __init__(s): s.messages = s
    def create(s, **kw):
        seen["kw"] = kw
        return NS(content=[NS(type="text", text=json.dumps(plan), citations=[])], stop_reason="end_turn", usage=NS(input_tokens=1500, output_tokens=900, server_tool_use=NS(web_search_requests=0)))
agents_ai.CLIENT = Fake()
hq.run_due(con, cfg, log=lambda *a: None)
g = hq.task(con, t["id"]) if hasattr(hq, "task") else dict(con.execute("SELECT * FROM hq_tasks WHERE id=?", (t["id"],)).fetchone())
g = g.get("task", g)
out = (json.loads(g["result"]) if isinstance(g.get("result"), str) else g["result"])["output"]
ok(g["status"] == "completed" and out["stages"][0]["steps"][0] == "Inquiry" and out["open_questions"], f"the plan comes back with records, pipelines, automations, phases and open questions ({g['status']})")
ok("No prices" in json.dumps(seen["kw"]) and "known=false" in json.dumps(seen["kw"]), "the Architect is told: no prices or promised results, and don't claim software they don't use")
print("Pitch")
ok("custom CRMs" in agents_ai.DEFAULT_OFFER and "where those tools allow it" in agents_ai.DEFAULT_OFFER, "AI Sidekicks' pitch says the team also builds custom CRMs (no integration promises)")
ins = agents_ai.SPECS["sdr.campaign_email"]["instructions"]
ok("also builds custom CRMs" in ins and "90 to 140 words" in ins, "intro emails mention custom CRMs in one short sentence")
ok(hq.di_subject("Bass River Properties") == "Custom AI Assistants & CRMs for Bass River Properties" and hq.di_subject("Universal Property Management Inc. (UPM Real Estate)") == "Custom AI Assistants & CRMs for Universal Property Management" and hq.di_subject("South Shore Property Management, LLC") == "Custom AI Assistants & CRMs for South Shore Property Management" and hq.di_subject("") == "Custom AI Assistants & CRMs for your team", "house subject line: Custom AI Assistants & CRMs for <short company name>")
ok(agents_ai.SPECS["sdr.campaign_email"].get("bare") and agents_ai.SPECS["sdr.campaign_email"].get("min_len") == 0, "the email writer only returns subject and body (no extra field to forget)")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
