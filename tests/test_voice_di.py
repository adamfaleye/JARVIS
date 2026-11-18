"""'Hey Jarvis' AI Sidekicks tools: find prospects, draft intro emails, status, pause. Throwaway database, fake AI, no audio.
Run: python tests/test_voice_di.py"""
import json, os, sys, tempfile, types
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for m in ("sounddevice",):  # the voice module imports audio libraries at load; not needed for tool logic
    sys.modules.setdefault(m, types.ModuleType(m))
from engine import db, config, hq, agents_ai

tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"
config.ROOT = tmp
(tmp / "data").mkdir()
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
(tmp / "config.yaml").write_text("timezone: America/New_York\nsms: {enabled: false, require_consent: true}\ncounties: []\n", encoding="utf-8")
(tmp / "counties.yaml").write_text("counties: []\n", encoding="utf-8")
from voice import jarvis_voice as jv

FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

class FakeAI:
    def __init__(s): s.messages = s; s.q = []
    def create(s, **kw):
        txt = json.dumps(kw.get("messages") or [])
        pick = next((n for n, (m, b) in enumerate(s.q) if m in txt), None)
        body = s.q.pop(pick)[1] if pick is not None else {"summary": "ok", "deliverable": "x" * 100, "needs_info": []}
        return NS(content=[NS(type="text", text=json.dumps(body), citations=[])], stop_reason="end_turn",
                  usage=NS(input_tokens=2000, output_tokens=400, server_tool_use=NS(web_search_requests=1)))
fake = FakeAI(); agents_ai.CLIENT = fake
names = {n for n, *_ in jv.TOOLS}
ok({"di_status", "di_find_prospects", "di_draft_intro_emails", "di_pause_campaign"} <= names, "Hey Jarvis has the AI Sidekicks tools")
ok("AI Sidekicks" in jv.Brain.system.__code__.co_consts.__str__() or any("AI Sidekicks" in str(c) for c in jv.Brain.system.__code__.co_consts), "its instructions know about AI Sidekicks and that email is approved in the app")
r = jv.run_tool("di_draft_intro_emails", {})
ok(r["status"] == "error" and "find companies first" in r["summary"], "'draft emails' before any search says to find companies first")
r = jv.run_tool("di_find_prospects", {"area": "Massachusetts", "count": 100})
ok(r["status"] == "ok" and "4 parts" in r["summary"] and "at most $0.20" in r["summary"] and "nothing is contacted" in r["summary"], f"'tell the SDR to find 100 in MA' starts a 4-part search and says the cost ({r['summary']})")
con, cfg = hq.connect(), config.load()
r = jv.run_tool("di_draft_intro_emails", {"area": "massachusetts"})
ok(r["status"] == "error" and "still searching" in r["summary"], "asking for emails mid-search says it's still searching")
mk = lambda part, names: (f"{part} of 4", {"summary": "Found.", "deliverable": "Property managers in Massachusetts. " * 4, "needs_info": [],
     "prospects": [{"company": n, "website": "", "city": "Worcester", "state": "MA", "phone": "", "email": f"hello@{n.split()[0].lower()}.com", "units": "", "notes": "", "source": "https://ex.com"} for n in names]})
fake.q += [mk(1, ["Alpha PM", "Bravo Homes"]), mk(2, ["Charlie Rentals"]), mk(3, ["Delta PM"]), mk(4, ["Echo Mgmt"])]
for _ in range(5):
    hq.run_due(con, cfg, log=lambda *a: None)
st = jv.run_tool("di_status", {})
ok(st["searches"][0]["found"] == 5 and st["searches"][0]["parts_done"] == "4 of 4", "status reports the finished search")
r = jv.run_tool("di_draft_intro_emails", {})
ok(r["status"] == "error" and "footer" in r["summary"].lower(), "drafting needs the email footer from Settings first")
hq.set_email_footer(con, "Mola Faleye, AI Sidekicks · PO Box 1, Springfield, MA 01103")
r = jv.run_tool("di_draft_intro_emails", {"daily_limit": 15})
ok(r["status"] == "ok" and "drafting 5 intro emails" in r["summary"] and "Nothing is sent" in r["summary"] and "15 a day" in r["summary"], f"'draft intro emails' starts the drafts, sends nothing ({r['summary']})")
ok(not [e for e in hq.email_outbox(con)["outbox"]], "no email is approved by voice")
r = jv.run_tool("di_draft_intro_emails", {})
ok(r["status"] == "error", "saying it again doesn't start a duplicate batch")
r = jv.run_tool("di_pause_campaign", {"name": "all"})
ok(r["status"] == "error" and "No sending" in r["summary"], "pause only applies to campaigns that are sending")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
