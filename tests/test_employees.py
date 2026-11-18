"""Talk to any employee + the Telegram phone line: role-scoped tools, nothing outward without approval, pairing,
approvals by reply, updates and quiet hours. No network, no AI spend (the model and Telegram are faked).
Run: python tests/test_employees.py"""
import json, sys, tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, dcrm, employees, telegram, agents_ai
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)
def raises(fn, text=""):
    try:
        fn()
    except ValueError as e:
        return text.lower() in str(e).lower()
    return False

con = hq.connect(); dc = dcrm.connect(); cfg = {}
dcrm.upsert(dc, {"company": "Lakeview Mechanical", "industry": "home services", "services": "HVAC", "city": "Millbury", "phone": "508-555-0180",
                 "contact_name": "Ray Lakeview", "contact_title": "Owner"}, check_email_dns=False)

print("Addressing an employee")
ok(employees.split_address("SDR: find 20 roofers in Lynn") == ("sdr", "find 20 roofers in Lynn"), "'SDR: ...' goes to the SDR")
ok(employees.split_address("hey hunter, best leads today?")[0] == "hunter", "'hey hunter, ...' goes to Hunter")
ok(employees.split_address("@ae prep my Lakeview call")[0] == "ae", "'@ae ...' goes to the AE")
ok(employees.split_address("Account Executive: draft it")[0] == "ae", "full titles work")
ok(employees.split_address("what's going on today?") == (None, "what's going on today?"), "no name: JARVIS takes it")
ok(employees.split_address("Note: call Bob at 3")[0] is None, "a word that isn't an employee isn't treated as one")

print("Each role gets its own tools; nothing outward")
names = lambda a: {t["name"] for t in employees.tools_for(a)}
ok({"research_businesses", "find_new_businesses", "build_linkedin_list", "assign_task"} <= names("sdr"), "SDR can research, find businesses, build the LinkedIn list")
sdr_kinds = next(t for t in employees.tools_for("sdr") if t["name"] == "assign_task")["input_schema"]["properties"]["kind"]["enum"]
ok("sdr.send_email" not in sdr_kinds and "sdr.campaign_email" in sdr_kinds, "SDR can draft but can't start a send")
ok("ae.book_meeting" not in json.dumps(employees.tools_for("ae")) and {"precall_brief", "draft_proposal"} <= names("ae"), "AE preps and drafts, can't book")
ok("search_crm" not in names("hunter") and "assign_task" in names("hunter"), "Hunter works MolaBucks leads, not the AI Sidekicks CRM")
ok("ask_employee" in names("jarvis") and "assign_task" not in names("jarvis"), "JARVIS hands work to the right employee")
ok(raises(lambda: employees.run_tool(con, cfg, "sdr", "assign_task", {"kind": "sdr.send_email", "request": "send it"}), "can't start"), "a send can't be started even if the model tries")

print("A conversation with tool use (model faked)")
class FakeClient:
    def __init__(self, script): self.script, self.calls = list(script), []
    @property
    def messages(self): return self
    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)
U = lambda i, o: NS(input_tokens=i, output_tokens=o)
step1 = NS(content=[NS(type="text", text="Checking."), NS(type="tool_use", id="t1", name="search_crm", input={"query": "Lakeview"})], usage=U(900, 60), stop_reason="tool_use")
step2 = NS(content=[NS(type="text", text="Lakeview Mechanical is in Millbury, owner Ray Lakeview. Call him at 508-555-0180.")], usage=U(1200, 40), stop_reason="end_turn")
fc = FakeClient([step1, step2])
before = agents_ai.spent(con)
r = employees.talk(con, cfg, "sdr", "who owns Lakeview?", client=fc)
ok("Ray Lakeview" in r["reply"] and r["name"] == "SDR", "the employee answers from the record")
ok("Lakeview Mechanical" in json.dumps(fc.calls[1]["messages"][-1]["content"]), "the tool result (real CRM data) went back to the model")
ok(agents_ai.spent(con) > before and r["cost"] > 0, "every call is priced and counted against the daily cap")
ok([c["role"] for c in hq.chats(con, "sdr")][-2:] == ["owner", "agent"], "the conversation is saved on the SDR's chat")
fc2 = FakeClient([NS(content=[NS(type="text", text="Yes, same Lakeview.")], usage=U(500, 10), stop_reason="end_turn")])
employees.talk(con, cfg, "sdr", "same one as before?", client=fc2)
ok(any("who owns Lakeview?" in str(m["content"]) for m in fc2.calls[0]["messages"]), "it remembers the recent conversation")
bad = FakeClient([NS(content=[NS(type="tool_use", id="x", name="assign_task", input={"kind": "sdr.send_email", "request": "send"})], usage=U(500, 20), stop_reason="tool_use"),
                  NS(content=[NS(type="text", text="I can't send emails myself; it needs your approval in the app.")], usage=U(600, 20), stop_reason="end_turn")])
r = employees.talk(con, cfg, "sdr", "send the Lakeview email now", client=bad)
ok("Error" in json.dumps(bad.calls[1]["messages"][-1]["content"]) and "approval" in r["reply"], "a refused action comes back as an error and the employee explains")
act = FakeClient([NS(content=[NS(type="tool_use", id="a", name="assign_task", input={"kind": "marketing.content_draft", "request": "3 LinkedIn posts about missed calls", "params": {"topic": "missed calls"}})], usage=U(500, 20), stop_reason="tool_use"),
                  NS(content=[NS(type="text", text="Drafting 3 posts now; I'll report back.")], usage=U(600, 20), stop_reason="end_turn")])
r = employees.talk(con, cfg, "marketing", "write 3 posts about missed calls", client=act)
t = hq.list_tasks(con, "marketing")[0]
ok(r["actions"] == ["assign_task"] and t["kind"] == "marketing.content_draft" and t["source"] == "owner_phone", "an allowed action starts a real task, marked as from your phone")
hq.chat_log(con, "sdr", "agent", "   ")
emp = FakeClient([NS(content=[NS(type="text", text=""), NS(type="tool_use", id="e1", name="search_crm", input={"query": "Lakeview"})], usage=U(500, 20), stop_reason="tool_use"),
                  NS(content=[NS(type="text", text="Found it.")], usage=U(600, 20), stop_reason="end_turn")])
r = employees.talk(con, cfg, "sdr", "find Lakeview again", client=emp)
def _empty_text(msgs):
    for m in msgs:
        c = m["content"]
        if isinstance(c, str) and not c.strip():
            return True
        if isinstance(c, list) and any(b.get("type") == "text" and not str(b.get("text", "")).strip() for b in c):
            return True
    return False
ok(r["reply"] == "Found it." and not any(_empty_text(c["messages"]) for c in emp.calls), "empty text never goes back to the API (it refuses it with a 400)")
agents_ai.set_cap(con, 0.0001) if hasattr(agents_ai, "set_cap") else db.kv_set(con, "ai_daily_cap", "0.0001")
r = employees.talk(con, cfg, "sdr", "anything new?", client=FakeClient([]))
ok("budget" in r["reply"].lower(), "over the daily cap it says so instead of spending")
db.kv_set(con, "ai_daily_cap", "5"); agents_ai.set_cap(con, 5) if hasattr(agents_ai, "set_cap") else None

print("Telegram pairing: only your chat")
sent = []
telegram.SEND = lambda chat, text: sent.append((chat, text))
st = telegram.status(con)
code = st["pair_code"]
ok(not st["paired"] and code and len(code) == 6, "a 6-digit pairing code shows in the app")
upd = lambda uid, chat, text: {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}
telegram.poll_once(con, cfg, talk=lambda *a, **k: {"name": "JARVIS", "reply": "x"}, updates=[upd(1, 111, "/start 000000"), upd(2, 222, "hello")])
ok(not telegram.status(con)["paired"] and not sent, "a wrong code or a stranger gets nothing")
telegram.poll_once(con, cfg, updates=[upd(3, 999, f"/start {code}")])
ok(telegram.status(con)["paired"] and sent and sent[-1][0] == "999" and "Paired" in sent[-1][1], "the right code pairs your chat")
sent.clear()
telegram.poll_once(con, cfg, talk=lambda *a, **k: {"name": "JARVIS", "reply": "x"}, updates=[upd(4, 222, "approvals")])
ok(not sent, "after pairing, other chats are still ignored")
asked = []
def fake_talk(con_, cfg_, aid, text, source=None):
    asked.append((aid, text, source)); return {"name": hq.AGENTS[aid]["name"], "reply": "On it."}
telegram.poll_once(con, cfg, talk=fake_talk, updates=[upd(5, 999, "SDR: find 20 roofers in Lynn"), upd(6, 999, "how's today looking?")])
ok(asked[0] == ("sdr", "find 20 roofers in Lynn", "owner_phone") and asked[1][0] == "jarvis", "messages go to the named employee, else JARVIS")
ok(sent[0][1] == "SDR: On it." and sent[1][1] == "On it.", "replies come back signed by the employee")

print("Approving from the phone")
t = hq.create_task(con, "sdr.send_email", {"to": "ray@lakeview.example", "subject": "Hi", "body": "Hi Ray,\n\nShort note.\n\nIf this isn't a fit, reply stop.",
                                          "prospect_id": "p1", "company": "Lakeview Mechanical"}, division="demigod")
lst = telegram.handle(con, cfg, "approvals")
sid = t["id"][-5:]
ok(sid in lst and "ray@lakeview.example" in lst, "the list shows each item with its id and who it goes to")
ok("don't have that id" in telegram.handle(con, cfg, "approve zzzzz"), "an unknown id is refused")
out = telegram.handle(con, cfg, f"approve {sid}")
ok("Approved" in out and hq.get_task(con, t["id"], events=False)["status"] != "awaiting_approval", "'approve <id>' approves exactly what you were shown")

print("The team reports in")
db.kv_set(con, "tg_seen", "")
telegram.notify_tick(con, cfg, now=datetime(2026, 11, 17, 10, 0))  # first look just records the baseline
sent.clear()
con.execute("INSERT INTO hq_email_replies(message_id,email_task,prospect_id,from_addr,at,text,class) VALUES(?,?,?,?,?,?,?)",
            ("m1", t["id"], "p1", "ray@lakeview.example", "2026-11-17T10:01", "Sounds good, call me Thursday", "interested")) if con.execute("PRAGMA table_info(hq_email_replies)").fetchall() else None
con.commit()
fin = hq.create_task(con, "marketing.content_draft", {"topic": "x", "request": "x"}, division="demigod", source="owner_phone")
con.execute("UPDATE hq_tasks SET status='completed', summary='3 posts drafted', updated=? WHERE id=?", (hq.now(), fin["id"])); con.commit()
msgs = telegram.notify_tick(con, cfg, now=datetime(2026, 11, 17, 10, 5))
ok(any("new reply from ray@lakeview.example" in m for m in msgs), "a new reply is sent to your phone")
ok(any("finished" in m and "3 posts drafted" in m for m in msgs), "work you asked for from the phone reports back when it's done")
sent.clear()
con.execute("INSERT INTO hq_email_replies(message_id,email_task,prospect_id,from_addr,at,text,class) VALUES(?,?,?,?,?,?,?)",
            ("m2", t["id"], "p2", "bob@x.example", "2026-11-17T22:00", "Out of office", "auto_reply")); con.commit()
msgs = telegram.notify_tick(con, cfg, now=datetime(2026, 11, 17, 22, 30))
ok(not msgs, "quiet hours hold routine updates")
msgs = telegram.notify_tick(con, cfg, now=datetime(2026, 11, 18, 9, 0))
ok(any("bob@x.example" in m for m in msgs), "held updates go out when quiet hours end")
ok("paused" in telegram.handle(con, cfg, "mute") and "back on" in telegram.handle(con, cfg, "unmute"), "mute and unmute")

print("Wiring")
ok({"telegram_status", "team"} <= hq.READ_OPS and {"employee_talk", "telegram_unpair"} <= hq.WRITE_OPS, "ops registered")
ok(len(hq.handle(con, cfg, {"op": "team"})["team"]) == len(hq.AGENTS), "the app can list the whole team")

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED"))
sys.exit(1 if FAILS else 0)
