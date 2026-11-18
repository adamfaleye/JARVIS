"""AI replies that aren't clean JSON: narration before it, a reply cut off at the token limit, and the repair turn.
Run: python tests/test_ai_json.py"""
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, agents_ai
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "t.db"
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

P = lambda n: {"company": f"Co {n}", "website": f"co{n}.com", "city": "Boston", "state": "MA", "phone": "", "email": f"hi@co{n}.com", "units": "", "notes": "", "source": f"https://co{n}.com"}
full = {"summary": "Found 3.", "deliverable": "Three property managers in Massachusetts with public contact pages.", "needs_info": [], "prospects": [P(1), P(2), P(3)]}
v, how = agents_ai._parse_answer("I'll search for property managers in Massachusetts.\n" + json.dumps(full))
ok(how == "ok" and len(v["prospects"]) == 3, "narration before the JSON is skipped")
cut = json.dumps(full)[:-60]
v, how = agents_ai._parse_answer(cut)
ok(how == "salvaged" and [p["company"] for p in v["prospects"]] == ["Co 1", "Co 2"], f"a reply cut off mid-list keeps its complete entries ({how})")
v, how = agents_ai._parse_answer('{"summary": "x", "deliverable": "a \\"quoted} brace\\" here", "needs_info": [], "prospects": [' + json.dumps(P(1)) + ', {"company": "Co 2", "web')
ok(how == "salvaged" and len(v["prospects"]) == 1 and "quoted} brace" in v["deliverable"], "braces inside strings don't confuse the salvage")
ok(agents_ai._parse_answer("Sorry, I couldn't find anything.")[1] == "invalid", "plain text with no JSON is still invalid")

class Fake:
    def __init__(s, replies): s.messages = s; s.replies = replies; s.calls = []
    def create(s, **kw):
        s.calls.append(kw)
        text, stop, blocks = s.replies.pop(0)
        content = (blocks or []) + [NS(type="text", text=text, citations=[])]
        return NS(content=content, stop_reason=stop, usage=NS(input_tokens=5000, output_tokens=900, server_tool_use=NS(web_search_requests=1 if blocks else 0)))
con = db.connect()
search_blocks = [NS(type="text", text='Searching {"draft": [ ', citations=[]), NS(type="server_tool_use"), NS(type="web_search_tool_result")]
agents_ai.CLIENT = Fake([(json.dumps(full)[:-60], "max_tokens", search_blocks)])
out, meta = agents_ai.ask(con, agent="sdr", kind="sdr.find_prospects", task_id="t1", business="demigod", tier="fast", system="s", instructions="i",
                          data={"area": "MA"}, output_spec="x", max_tokens=8000, web_search=5)
ok(out and len(out["prospects"]) == 2 and meta.get("cut_off") and meta.get("json") == "salvaged" and len(agents_ai.CLIENT.calls) == 1, "a cut-off web-search reply is salvaged without another call (narration with a stray brace before the search is ignored)")
agents_ai.CLIENT = Fake([("Here you go: the list is long", "end_turn", search_blocks), (json.dumps(full), "end_turn", None)])
out, meta = agents_ai.ask(con, agent="sdr", kind="sdr.find_prospects", task_id="t2", business="demigod", tier="fast", system="s", instructions="i",
                          data={"area": "MA"}, output_spec="x", max_tokens=8000, web_search=5)
rep = agents_ai.CLIENT.calls[1]
ok(out and len(out["prospects"]) == 3 and meta.get("json") == "repaired" and "tools" not in rep and meta["input_tokens"] == 10000, "otherwise one repair turn (no web search) rewrites it as JSON, and its cost is counted")
row = con.execute("SELECT input_tokens FROM hq_ai_usage WHERE task_id='t2'").fetchone()
ok(row["input_tokens"] == 10000, "the repair's tokens are in the spend log")
ok(agents_ai.SPECS["sdr.find_prospects"]["max_tokens"] >= 8000, "the web search agent has room for 25 companies (was 3,000 tokens, which cut it off)")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
