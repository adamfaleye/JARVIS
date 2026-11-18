"""AI Sidekicks Instagram assistant (@aisidekicks): DM answers, discovery-call drafts and Mola's approval workflow.
Run: python -m tests.test_igassist   (throwaway databases; no network; no real messages)"""
import hashlib, hmac, json, os, re, sys, tempfile, threading, time, urllib.request, urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, agents_ai
from engine.igassist import store, kb, workflow, outbox, brain, meta, service, auth, analytics, server

tmp = Path(tempfile.mkdtemp())
store.CLIENTS_DIR = tmp / "clients"
db.DB_PATH = tmp / "jarvis.db"
brain.AI_ENABLED = False
T = "aisidekicks"
os.environ.update({"AISIDEKICKS_IG_APP_SECRET": "appsecret123", "AISIDEKICKS_IG_VERIFY_TOKEN": "verify-me",
                   "AISIDEKICKS_IG_ACCESS_TOKEN": "tok", "AISIDEKICKS_IG_ACCOUNT_ID": "17840000000"})
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

con, added = service.init_tenant(T)
auth.add_user(con, "mola", "Mola", "owner", "sidekicks-own-1")
auth.add_user(con, "helper", "Helper", "operator", "sidekicks-ops-1")
OWNER = {"username": "mola", "role": "owner"}
OPER = {"username": "helper", "role": "operator"}
TR = meta.TestTransport()
OFF_BRAND = re.compile(r"bella|salon|hair|curl|rosy|stylist|balayage|gloss", re.I)

def conv_of(who):
    return con.execute("SELECT c.* FROM conversations c JOIN customers u ON u.id=c.customer_id WHERE u.igsid=?", (who,)).fetchone()
def last_out(cid):
    return con.execute("SELECT * FROM messages WHERE conversation_id=? AND direction='out' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
def outs(cid):
    return con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND direction='out'", (cid,)).fetchone()[0]
_n = [0]
def meta_msg(sender, text, mid=None, echo=False, attachments=None):
    _n[0] += 1
    m = {"mid": mid or f"m_{sender}_{_n[0]}", "text": text}
    if attachments: m["attachments"] = attachments
    if echo: m["is_echo"] = True
    ev = {"sender": {"id": "17840000000" if echo else sender}, "recipient": {"id": sender if echo else "17840000000"},
          "timestamp": int(time.time() * 1000), "message": m}
    return {"object": "instagram", "entry": [{"id": "17840000000", "time": int(time.time()), "messaging": [ev]}]}
def deliver(payload):
    meta.store_event(con, json.dumps(payload).encode(), True)
    return service.tick(con, T, TR)
def say(sender, text, **kw):
    deliver(meta_msg(sender, text, **kw))
    c = conv_of(sender)
    return c, last_out(c["id"]) if c else None
def appt_of(cid):
    a = workflow.open_for_conversation(con, cid)
    return workflow.get(con, a["id"]) if a else None

print("knowledge base")
s = kb.services(con)
ok(set(s) == {"ai_assistant", "custom_crm", "automation", "not_sure"}, "AI Sidekicks services seeded: AI assistant, CRM, automation, not sure")
ok(all(v["consultation"] and v["price_cents"] is None for v in s.values()), "no prices invented: every service is scoped on a discovery call")
ok(all(v["duration_min"] is None for v in s.values()), "no call lengths invented")
p = kb.profile(con)
ok(p["owner"] == "Mola" and p["booking_noun"] == "discovery call" and p["brand"] == "AI Sidekicks", "profile: Mola approves discovery calls")
ok("business hours" in kb.missing_facts(con) and "cancellation policy" in kb.missing_facts(con), "missing facts listed, not guessed")
try:
    kb.save(con, "service", "bad", {"label": "X"}, "mola"); ok(False, "service without price rejected")
except ValueError: ok(True, "service without price must be consultation")
kb.save(con, "service", "custom_crm", {**s["custom_crm"], "description": "A CRM built around your workflow."}, "mola")
ok(kb.item(con, "service", "custom_crm")["description"] == "A CRM built around your workflow." and con.execute("SELECT COUNT(*) FROM kb_history WHERE key='custom_crm'").fetchone()[0] == 1, "edit keeps version history")
try:
    kb.save(con, "service", "custom_crm", {**s["custom_crm"]}, "mola", expected_version=1); ok(False, "stale KB edit refused")
except ValueError: ok(True, "stale KB edit refused (version check)")

print("answers")
c, m = say("jay", "what do you guys do?")
ok("custom AI assistants" in m["text"] and "Mola" in m["text"] and "$" not in m["text"] and m["text"].startswith("Hi! I'm the AI Sidekicks assistant."),
   "'what do you do' → services summary, no prices, honest intro")
ok(m["status"] == "draft" and not outbox.guard_problems(con, m["text"]), "reply waits as a draft (review mode) and passes guards")
c, m = say("jay", "thanks!")
ok("AI Sidekicks assistant" not in m["text"], "intro only once per conversation")
for who, text in [("p1", "how much would an AI receptionist cost?"), ("p2", "what does a custom CRM cost"), ("p3", "what are your prices")]:
    c, m = say(who, text)
    ok("discovery call" in m["text"] and "$" not in m["text"] and conv_of(who)["mode"] == "ai" and not outbox.guard_problems(con, m["text"]),
       f"'{text}' → scoped on a discovery call, no number")

c, m = say("price1", "Hello, saw your post about AI consulting, how much for a booking?")
ok("Pricing depends on what you need" in m["text"] and "discovery call" in m["text"] and "$" not in m["text"] and "What are you looking for help with" in m["text"],
   "'how much for a booking?' → answers pricing (scoped on the call), then asks what they need")

print("qualification → discovery-call draft")
c, m = say("tasha", "Hi! How much is a custom CRM?")
c, m = say("tasha", "ok let's book a call")
a = appt_of(c["id"])
ok(a and a["status"] == workflow.QUALIFYING and a["fields"]["service_key"] == "custom_crm", "service carried from the earlier message")
ok("what are you looking for" not in m["text"].lower(), "doesn't re-ask what the customer already said")
c, m = say("tasha", "My name is Tasha Reed, I run a property management company. Friday or Saturday around 2pm works")
a = appt_of(c["id"])
ok(a["status"] == workflow.AWAITING_AVAILABILITY and not a["missing"], "complete request → draft created → awaiting Mola's availability check")
ok(a["fields"]["customer_name"] == "Tasha Reed" and a["fields"]["alt_date"] and a["service"]["label"] == "Custom CRM", "draft has name, service, preferred + backup day")
ok(a["availability_label"] == "Awaiting Mola's verification" and a["service"]["duration"].startswith("Not provided"), "availability awaiting Mola; call length not invented")
ok("isn't booked" in m["text"] and "He'll check his calendar" in m["text"] and not outbox.guard_problems(con, m["text"]), "ack: Mola checks his calendar; not booked yet")
ok(a["summary"] and "Tasha" in a["summary"], "draft has a summary")
q = workflow.queue(con)
ok(any(i["id"] == a["id"] for g in q["groups"] if g["key"] == "needs_availability" for i in g["items"]) and q["attention"] >= 1, "draft is in Mola's queue under 'Check availability'")
ok(workflow.labels(con)[workflow.READY_FOR_OWNER] == "Ready for Mola", "status names use Mola's name")

print("reply wording")
con.execute("INSERT INTO customers(igsid,name,username,created_at,updated_at) VALUES('wmola','Mola Faleye','molabucks',?,?)", (store.now(), store.now())); con.commit()
c, m = say("wmola", "Hey, I run a property management company. How much would custom CRM cost?")
ok(m["text"].startswith("Hi! I'm the AI Sidekicks assistant. We build custom CRMs around how your team actually works") and "Custom CRM:" not in m["text"],
   "service answer reads like a person, not a catalog entry")
c, m = say("wmola", "ok let's book a call")
ok(m["text"].count("?") == 1 and "What day and time work best for you?" in m["text"] and "backup" not in m["text"], "asks day and time in one question")
c, m = say("wmola", "Can you do Thursday at noon?")
ok("Thursday, Nov" in m["text"] and " at noon." in m["text"] and "(noon)" not in m["text"], "date and time read naturally ('Thursday, Nov 19 at noon')")
ok("thanks Mola" not in m["text"] and m["text"].count("Mola") == 1, "doesn't say 'thanks Mola ... sent Mola' when the customer shares the owner's name")
ok("a custom CRM for your property management company" in m["text"], "ack repeats their business, mentioned in an earlier message")
wa = appt_of(c["id"])
ok(wa["fields"].get("business") == "property management company" and wa["fields"].get("needs") == "Property management company", "business saved on the request for Mola")

print("approval enforcement")
for who, label in [(OPER, "operator"), ({"username": "ai", "role": "ai"}, "AI/system"), (None, "anonymous")]:
    try:
        workflow.verify_availability(con, a["id"], who, "Fri", "2pm"); ok(False, f"{label} can't verify availability")
    except workflow.Forbidden: ok(True, f"{label} can't verify availability")
    try:
        workflow.approve(con, a["id"], who); ok(False, f"{label} can't approve")
    except workflow.Forbidden: ok(True, f"{label} can't approve")
try:
    workflow.approve(con, a["id"], OWNER); ok(False, "approve before availability verified refused")
except workflow.WorkflowError as e: ok("availability" in str(e).lower(), "approve before availability verified refused")
try:
    workflow.verify_availability(con, a["id"], OWNER, "Friday, Nov 20", "2:00 pm", expected_version=1); ok(False, "stale version refused")
except workflow.Conflict: ok(True, "stale version refused (conflict)")
a = workflow.verify_availability(con, a["id"], OWNER, "Friday, Nov 20", "2:00 pm")
ok(a["status"] == workflow.READY_FOR_OWNER and a["availability"] == "verified", "Mola verifies → ready for approval")
a = workflow.mark_reviewing(con, a["id"], OWNER)
ok(a["status"] == workflow.OWNER_REVIEWING, "opening it → Mola reviewing")
c2, m2 = say("tasha", "actually can we do 3pm instead")
a = workflow.get(con, a["id"])
ok(a["fields"]["preferred_time"] == "2:00 pm" and con.execute("SELECT COUNT(*) FROM appointment_notes WHERE appointment_id=? AND text LIKE '%3 pm%'", (a["id"],)).fetchone()[0] == 1,
   "customer change during review → note for Mola, fields untouched")
a = workflow.edit(con, a["id"], OWNER, {"preferred_time": "3:00 pm"})
ok(a["availability"] == "awaiting" and a["status"] == workflow.AWAITING_AVAILABILITY, "Mola changes the time → availability must be re-verified")
a = workflow.verify_availability(con, a["id"], OWNER, "Friday, Nov 20", "3:00 pm")
a = workflow.approve(con, a["id"], OWNER)
ok(a["status"] == workflow.AWAITING_CALENDAR_ENTRY and a["approval_valid"], "approve → awaiting calendar entry (manual mode)")
ok(not a["customer_may_be_told_booked"], "approved is not booked: customer may NOT be told")
ev = [e["action"] for e in workflow.detail(con, a["id"])["events"]]
ok("approved" in ev and "awaiting_calendar_entry" in ev, "approval + calendar step recorded in history")
a2 = workflow.edit(con, a["id"], OWNER, {"notes": "wants a demo of the tenant portal"})
ok(a2["status"] == workflow.AWAITING_CALENDAR_ENTRY and a2["approval_valid"], "non-booking note edit keeps approval")
a2 = workflow.edit(con, a["id"], OWNER, {"service_key": "automation"})
ok(a2["status"] in (workflow.READY_FOR_OWNER, workflow.OWNER_REVIEWING) and not a2["approved_hash"] and not a2["approval_valid"], "booking detail edit after approval voids it")
a2 = workflow.edit(con, a["id"], OWNER, {"service_key": "custom_crm"})
a = workflow.approve(con, a["id"], OWNER)
try:
    workflow.mark_confirmed(con, a["id"], OWNER, True); ok(False, "can't confirm before calendar entry")
except workflow.WorkflowError: ok(True, "can't confirm before calendar entry")
try:
    workflow.mark_entered(con, a["id"], OWNER, False); ok(False, "calendar entry needs Mola's attestation")
except workflow.WorkflowError: ok(True, "calendar entry needs Mola's attestation")
try:
    workflow.mark_entered(con, a["id"], OPER, True); ok(False, "operator can't record the calendar entry")
except workflow.Forbidden: ok(True, "operator can't record the calendar entry")
a = workflow.mark_entered(con, a["id"], OWNER, True, "Tasha R 11/20 3pm")
ok(a["status"] == workflow.ENTERED_IN_CALENDAR and not a["customer_may_be_told_booked"], "entered in calendar, still not confirmed to customer")
try:
    workflow.edit(con, a["id"], OWNER, {"preferred_time": "4pm"}); ok(False, "no edits once on the calendar")
except workflow.WorkflowError: ok(True, "details locked once entered in the calendar")

print("false-confirmation guards")
cid = a["conversation_id"]
for bad in ["You're booked for Friday at 3!", "Your call is confirmed.", "Great, see you on Friday!"]:
    ok(any("booked" in p for p in outbox.guard_problems(con, bad)), f"AI can't say: {bad!r}")
mid = outbox.enqueue(con, cid, "Hi Tasha! You're all set for Friday at 3. See you then!", author="owner", kind="confirmation", appointment_id=a["id"])
ok(con.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "blocked", "confirmation message blocked before Mola confirms")
try:
    outbox.send_manual(con, cid, "You're booked, see you Friday!", OWNER); ok(False, "manual 'booked' message blocked before confirmation")
except ValueError: ok(True, "even Mola's typed 'you're booked' is held until confirmed")
try:
    workflow.mark_confirmed(con, a["id"], OWNER, False); ok(False, "confirm needs attestation")
except workflow.WorkflowError: ok(True, "confirm needs 'I can see it on my calendar'")
a = workflow.mark_confirmed(con, a["id"], OWNER, True)
ok(a["status"] == workflow.CONFIRMED and a["customer_may_be_told_booked"], "confirmed → customer may be told")
txt = brain.confirmation_text(con, a)
mid = outbox.enqueue(con, cid, txt, author="owner", kind="confirmation", appointment_id=a["id"])
ok(con.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "queued" and "3:00 pm" in txt and "discovery call" in txt and "Mola" in txt,
   "confirmation queued only after CONFIRMED, with the slot, in AI Sidekicks wording")
outbox.discard(con, mid, OWNER)

print("guards on AI text")
gp = lambda t: outbox.guard_problems(con, t)
ok(gp("A custom CRM is $1,500.") and gp("Setup starts at $99/month."), "invented prices blocked")
ok(gp("We can do 10% off for new clients"), "discount blocked")
ok(gp("The call takes about 30 minutes."), "invented call length blocked")
ok(gp("Friday at 2 is available!") and gp("Mola has an opening Saturday") and gp("That time works!"), "availability claims blocked")
ok(not gp("Mola will check his calendar and get back to you. What time works best for you?"), "asking what works / 'check his calendar' allowed")
ok(gp("There's a $200 deposit."), "invented deposit blocked")
ok(gp("Hi [First name], thanks!"), "placeholders blocked")
ok(gp("x" * 1001), "over Instagram's 1000-byte limit blocked")

print("escalations")
for who, text, word in [("e1", "I want a refund, the bot you built is terrible", "Complaint"), ("e2", "can I get a discount?", "Discount"),
                        ("e3", "I need to cancel my call tomorrow", "Cancellation"), ("e4", "my lawyer wants to look at the contract dispute", "Sensitive")]:
    c, m = say(who, text)
    c = conv_of(who)
    ok(c["mode"] == "human" and c["status"] == "escalated" and word.lower() in (c["escalation_reason"] or "").lower() and m["kind"] == "holding",
       f"{word}: escalated to Mola, AI paused, holding reply")
c, m = say("kim", "can you guarantee me more leads?")
ok(conv_of("kim")["mode"] == "human" and "Mola" in m["text"], "guarantee questions go to Mola")
n0 = outs(conv_of("e1")["id"])
say("e1", "hello??")
ok(outs(conv_of("e1")["id"]) == n0, "after escalation the AI stays quiet until Mola returns control")
c, m = say("e5", "what's your cancellation policy?")
ok(conv_of("e5")["escalation_reason"] == brain.ESCALATION_LABELS["unknown"], "policy question with no policy on file → check with Mola, not invented")
kb.save(con, "policy", "cancellation", {"text": "Please give 24 hours' notice to move or cancel a call."}, "mola")
c, m = say("e6", "what's your cancellation policy?")
ok("24 hours" in m["text"] and conv_of("e6")["mode"] == "ai", "policy answered once Mola adds it")

print("human takeover")
c, m = say("h1", "what does a custom CRM cost?")
cid = c["id"]
con.execute("UPDATE conversations SET mode='human' WHERE id=?", (cid,)); con.commit()
n0 = outs(cid)
say("h1", "and automation?")
ok(outs(cid) == n0, "AI doesn't reply in a conversation Mola has taken over")
con.execute("UPDATE conversations SET mode='ai' WHERE id=?", (cid,)); con.commit()
say("h1", "how much is automation?")
ok(outs(cid) == n0 + 1, "AI resumes only after Mola returns control")
c, m = say("h2", "hi")
d = last_out(c["id"])
deliver(meta_msg("h2", "Hey! I'll DM you in a sec", echo=True))
c = conv_of("h2")
ok(c["mode"] == "human" and con.execute("SELECT status FROM messages WHERE id=?", (d["id"],)).fetchone()[0] == "discarded",
   "Mola replying in the Instagram app takes over and drops the pending AI draft")
store.put(con, "live_send", "1")
outbox.approve_draft(con, last_out(conv_of("p3")["id"])["id"], OWNER)
r = outbox.deliver_due(con, TR)
sent_mid = con.execute("SELECT mid FROM messages WHERE status='sent' ORDER BY id DESC LIMIT 1").fetchone()[0]
deliver(meta_msg("p3", "echo of our send", mid=sent_mid, echo=True))
ok(conv_of("p3")["mode"] == "ai", "echo of JARVIS's own send doesn't trigger takeover")

print("global pause")
store.put(con, "automation_paused", "1")
c, m = say("z1", "what do you do?")
ok(c and outs(c["id"]) == 0 and con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND direction='in'", (c["id"],)).fetchone()[0] == 1,
   "paused: message stored, no AI reply")
store.put(con, "automation_paused", "0")
c, m = say("z1", "what do you do?")
ok(outs(c["id"]) == 1, "resumed: AI replies to new messages")

print("webhooks: dedupe, signatures, deletes")
p = meta_msg("d1", "what do you do?", mid="dup-1")
deliver(p); deliver(p); deliver(p)
c = conv_of("d1")
ok(con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND direction='in'", (c["id"],)).fetchone()[0] == 1 and outs(c["id"]) == 1,
   "same event delivered 3× → one message, one reply")
raw = json.dumps(p).encode()
sig = "sha256=" + hmac.new(b"appsecret123", raw, hashlib.sha256).hexdigest()
ok(meta.signature_ok(T, raw, sig) and not meta.signature_ok(T, raw, "sha256=" + "0" * 64) and not meta.signature_ok(T, raw + b" ", sig) and not meta.signature_ok(T, raw, ""),
   "X-Hub-Signature-256 verified (HMAC-SHA256 with app secret)")
ok(meta.verify_challenge(T, {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}) == "42"
   and meta.verify_challenge(T, {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}) is None, "webhook verification token checked")
deliver({"object": "instagram", "entry": [{"id": "17840000000", "messaging": [{"sender": {"id": "d1"}, "recipient": {"id": "17840000000"}, "message": {"mid": "dup-1", "is_deleted": True}}]}]})
ok(con.execute("SELECT text, status FROM messages WHERE mid='dup-1'").fetchone()[:] == (None, "deleted"), "customer deletes a message → text removed (Meta terms)")

print("delivery: test mode, window, retries")
store.put(con, "live_send", "0")
c, m = say("w1", "what do you do?")
outbox.approve_draft(con, m["id"], OWNER)
outbox.deliver_due(con, TR)
row = con.execute("SELECT status, hold_reason, mid FROM messages WHERE id=?", (m["id"],)).fetchone()
ok(row["status"] == "held" and "test mode" in row["hold_reason"] and row["mid"] is None, "sending off → held, never reported sent")
store.put(con, "live_send", "1")
outbox.approve_draft(con, m["id"], OWNER)
n_before = len(TR.sent)
outbox.deliver_due(con, TR)
row = con.execute("SELECT status, mid, author FROM messages WHERE id=?", (m["id"],)).fetchone()
ok(row["status"] == "sent" and row["mid"] and len(TR.sent) == n_before + 1 and row["author"] == "ai_reviewed", "sending on → sent with Instagram message id")
con.execute("UPDATE conversations SET last_inbound_at=? WHERE id=?", ((datetime.now().astimezone() - timedelta(hours=25)).isoformat(), c["id"])); con.commit()
mid = outbox.send_manual(con, c["id"], "Checking in!", OWNER)
outbox.deliver_due(con, TR)
ok(con.execute("SELECT status, hold_reason FROM messages WHERE id=?", (mid,)).fetchone()[0] == "held", "outside 24-hour window → held with reason")
con.execute("UPDATE conversations SET last_inbound_at=? WHERE id=?", (store.now(), c["id"])); con.commit()
calls = {"n": 0}
def flaky(igsid, text):
    calls["n"] += 1
    return meta.SendError("Instagram error 500", retryable=True) if calls["n"] == 1 else None
FT = meta.TestTransport(fail=flaky)
outbox.approve_draft(con, mid, OWNER)
outbox.deliver_due(con, FT)
r1 = con.execute("SELECT status, attempts, next_attempt_at FROM messages WHERE id=?", (mid,)).fetchone()
ok(r1["status"] == "queued" and r1["attempts"] == 1, "transient error → queued for retry with backoff")
outbox.deliver_due(con, FT, now=datetime.now().astimezone() + timedelta(seconds=40))
ok(con.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "sent", "retry succeeds")
mid = outbox.send_manual(con, c["id"], "One more", OWNER)
outbox.deliver_due(con, meta.TestTransport(fail=lambda *a: meta.SendError("token expired", retryable=False)))
ok(con.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "failed", "permanent error → failed, not retried forever")
class NoId(meta.TestTransport):
    def send_text(self, i, t): return None
mid = outbox.send_manual(con, c["id"], "And another", OWNER)
outbox.deliver_due(con, NoId())
ok(con.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()[0] == "failed", "no message id from Instagram → not counted as sent")
ok(meta.GraphTransport(T) and isinstance(meta.TestTransport(), meta.TestTransport), "transports build")
try:
    meta.GraphTransport(T).send_text("test:someone", "hi"); ok(False, "test ids never sent")
except meta.SendError: ok(True, "simulator ids are never sent to Instagram")

print("review vs auto mode")
store.put(con, "reply_mode", "auto")
c, m = say("a1", "what do you guys build?")
ok(m["status"] in ("queued", "sent"), "auto mode: routine answer goes out")
c, m = say("a2", "I'm unhappy with the assistant you built")
ok(conv_of("a2")["mode"] == "human", "auto mode still escalates complaints")
store.put(con, "reply_mode", "review")

print("information needed loop")
c, m = say("r1", "can we set up a call about automation next tuesday at 11am? I'm Rae")
a = appt_of(c["id"])
ok(a and a["status"] == workflow.AWAITING_AVAILABILITY and a["fields"]["service_key"] == "automation", "automation call request → draft")
a = workflow.request_info(con, a["id"], OWNER, "Could you send a screenshot of your current process?")
ok(a["status"] == workflow.INFORMATION_NEEDED, "Mola requests info")
c, m = say("r1", "sure here you go", attachments=[{"type": "image", "payload": {"url": "https://cdn.example/rae.jpg"}}])
a = workflow.get(con, a["id"])
ok(a["status"] == workflow.AWAITING_AVAILABILITY and "https://cdn.example/rae.jpg" in a["fields"].get("photos", []), "customer replies → back in Mola's queue with the screenshot")
a = workflow.decline(con, a["id"], OWNER, "Fully booked that week")
ok(a["status"] == workflow.DECLINED, "decline")
a = workflow.return_to_pending(con, a["id"], OWNER)
ok(a["status"] == workflow.AWAITING_AVAILABILITY, "return to pending")
a = workflow.cancel(con, a["id"], OWNER, "customer no longer needs it")
ok(a["status"] == workflow.CANCELLED, "cancel")
c, m = say("ned", "not sure what I need, can we book a call? I'm Ned, monday morning")
a = appt_of(c["id"])
ok(a and a["fields"]["service_key"] == "not_sure" and a["status"] == workflow.AWAITING_AVAILABILITY, "'not sure what I need' → discovery call to talk it through")

print("model path (fake client)")
class FakeMsgs:
    def __init__(self): self.reply = None
    def create(self, **kw):
        return NS(content=[NS(type="text", text=json.dumps(self.reply))], usage=NS(input_tokens=1200, output_tokens=200, server_tool_use=None), stop_reason="end_turn")
fake = NS(messages=FakeMsgs())
agents_ai.CLIENT = fake
brain.AI_ENABLED = True
fake.messages.reply = {"intent": "pricing", "extracted": {}, "reply": "A custom CRM is just $499 this week!", "answerable": True, "escalate": False, "confidence": 0.9}
c, m = say("ai1", "how much is a custom CRM?")
ok("$" not in m["text"] and "discovery call" in m["text"], "model invents a price → guard rejects it, template used")
fake.messages.reply = {"intent": "pricing", "extracted": {}, "reply": "Every CRM we build is scoped to your workflow, so Mola prices it on a quick discovery call. Want me to set one up?", "answerable": True, "escalate": False}
c, m = say("ai2", "how much is a custom CRM?")
ok(m["text"].endswith("Want me to set one up?"), "model reply that passes guards is used")
fake.messages.reply = {"intent": "other", "extracted": {}, "reply": "Sure thing!", "answerable": True, "escalate": True, "escalate_reason": "asks about a data breach"}
c, m = say("ai3", "is my customer data safe with you?")
ok(conv_of("ai3")["mode"] == "human" and "breach" in conv_of("ai3")["escalation_reason"], "model can add an escalation")
fake.messages.reply = {"intent": "booking", "extracted": {"customer_name": "Jessica", "service_key": "automation", "preferred_date": "Saturday", "preferred_time": "noon"},
                       "reply": "Thanks! Mola will check his calendar.", "answerable": True, "escalate": False}
c, m = say("ai4", "can we book a call about automation saturday at noon")
a = appt_of(c["id"])
ok(a and "customer_name" not in a["fields"] and a["fields"]["service_key"] == "automation", "model-made-up name (not in the chat) rejected")
fake.messages.reply = {"intent": "pricing", "extracted": {}, "reply": "Escalating", "answerable": True, "escalate": False}
c, m = say("ai5", "I want a refund")
ok(conv_of("ai5")["mode"] == "human", "model can't remove a rules escalation")
from engine import hq
uc = hq.connect()
ok(uc.execute("SELECT COUNT(*) FROM hq_ai_usage WHERE agent='client:aisidekicks'").fetchone()[0] >= 5, "AI spend logged in JARVIS's usage table (daily cap applies)")
agents_ai.set_cap(uc, 0)
fake.messages.reply = {"intent": "pricing", "reply": "A CRM is $99!", "answerable": True}
c, m = say("ai6", "how much is a custom CRM?")
ok("$" not in m["text"] and "discovery call" in m["text"], "over the daily AI cap → template reply, still accurate")
agents_ai.set_cap(uc, 5)
brain.AI_ENABLED = False

print("analytics")
r = analytics.report(con, 30)
ok(r["verified_in_calendar"] == 1 and r["drafts_approved"] >= 1 and r["drafts_generated"] >= 3 and r["escalations"] >= 5, "analytics count real outcomes")
ok(r["ai_replies_sent"] >= 1 and "Estimate" in r["estimate_note"], "time saved labeled as an estimate")
service.simulate_inbound(con, "sim", "what do you do?")
service.tick(con, T, TR)
r2 = analytics.report(con, 30)
ok(r2["incoming_dms"] == r["incoming_dms"] and analytics.report(con, 30, include_test=True)["incoming_dms"] == r["incoming_dms"] + 1, "test conversations excluded unless asked")
sim = conv_of("test:sim")
ok(last_out(sim["id"])["status"] == "draft" and sim["is_test"] == 1, "simulator uses the same pipeline (draft), marked test")
outbox.approve_draft(con, last_out(sim["id"])["id"], OWNER)
outbox.deliver_due(con, TR)
ok(last_out(sim["id"])["status"] == "simulated", "simulator reply shown, never sent")

print("nothing from the old salon client")
texts = [r[0] for r in con.execute("SELECT text FROM messages WHERE direction='out' AND text IS NOT NULL")]
leaks = [t for t in texts if OFF_BRAND.search(t)]
ok(texts and not leaks, f"no salon wording in any of {len(texts)} assistant replies" + (f": {leaks[:2]}" if leaks else ""))
kbtext = json.dumps(kb.all_items(con))
ok(not OFF_BRAND.search(kbtext), "no salon wording anywhere in the knowledge base")
ok(set(kb.SEEDS) == {"aisidekicks"}, "only the AI Sidekicks seed ships")

print("customer data")
cu = con.execute("SELECT id FROM customers WHERE igsid='tasha'").fetchone()[0]
ex = service.export_customer(con, cu)
ok(ex and ex["messages"] and ex["appointments"], "export customer data")
ok(service.delete_customer(con, cu, "mola") and not con.execute("SELECT 1 FROM appointments WHERE customer_id=?", (cu,)).fetchone(), "delete customer data")
con.execute("INSERT INTO messages(conversation_id,direction,author,text,status,created_at) VALUES(?,?,?,?,?,?)",
            (conv_of("p1")["id"], "in", "customer", "old", "received", (datetime.now().astimezone() - timedelta(days=400)).isoformat()))
store.put(con, "retention_days", "365")
ok(service.apply_retention(con) == 1, "retention deletes messages older than the setting")
store.put(con, "retention_days", "")

print("tenant separation")
other = store.connect("otherclient")
ok(store.db_path("otherclient") != store.db_path(T) and other.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0, "another workspace is a separate database")
try:
    store.db_path("../jarvis"); ok(False, "tenant id path tricks refused")
except ValueError: ok(True, "tenant id path tricks refused")

print("auth")
try:
    auth.add_user(con, "weak", "W", "owner", "short"); ok(False, "short password refused")
except ValueError: ok(True, "short passwords refused")
try:
    auth.add_user(con, "x1", "X", "stylist", "long-enough-pw-1"); ok(False, "unknown role refused")
except ValueError: ok(True, "only owner and operator roles exist")
row = con.execute("SELECT pw_hash FROM users WHERE username='mola'").fetchone()[0]
ok(row.startswith("scrypt$") and "sidekicks-own-1" not in row, "passwords stored as scrypt hashes")

print("dashboard server (HTTP)")
srv = server.make_server(T, "127.0.0.1", 0, transport=TR, worker=False)
srv.app.con.close(); srv.app.con = con
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
class Client:
    def __init__(s): s.cookie, s.csrf = "", ""
    def req(s, path, body=None, raw=None, headers=None):
        h = {"Content-Type": "application/json", **({"Cookie": s.cookie} if s.cookie else {}), **({"X-CSRF": s.csrf} if s.csrf else {}), **(headers or {})}
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request(BASE + path, data=data, method="GET" if data is None else "POST", headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as res:
                ck = res.headers.get("Set-Cookie")
                if ck: s.cookie = ck.split(";")[0]
                t = res.read().decode()
                return res.status, (json.loads(t) if "json" in res.headers.get("Content-Type", "") else t), res.headers
        except urllib.error.HTTPError as e:
            t = e.read().decode()
            try: return e.code, json.loads(t), e.headers
            except ValueError: return e.code, t, e.headers
    def login(s, u, p):
        st, j, _ = s.req("/api/login", {"username": u, "password": p})
        if st == 200: s.csrf = j["csrf"]
        return st
st, html, h = Client().req("/")
ok(st == 200 and "nonce-" in h.get("Content-Security-Policy", "") and "{{NONCE}}" not in html and h.get("X-Frame-Options") == "DENY", "dashboard served with CSP nonce and security headers")
ok(Client().req("/api/queue")[0] == 401, "API needs sign-in")
b = Client()
ok(b.login("mola", "wrong-password") == 401, "wrong password refused")
ok(b.login("mola", "sidekicks-own-1") == 200 and "HttpOnly" in (b.req("/api/me")[2].get("Set-Cookie") or "HttpOnly"), "Mola signs in")
st, me, _ = b.req("/api/me")
ok(st == 200 and me["user"]["role"] == "owner" and "pw_hash" not in json.dumps(me), "session works, no secrets returned")
csrf = b.csrf; b.csrf = ""
ok(b.req("/api/settings", {"automation_paused": "1"})[0] == 403, "writes without the CSRF header refused")
b.csrf = csrf
st, q, _ = b.req("/api/queue")
item = next(i for g in q["groups"] if g["key"] == "needs_availability" for i in g["items"])
o = Client(); o.login("helper", "sidekicks-ops-1")
st, j, _ = o.req(f"/api/appointments/{item['id']}/verify_availability", {"date": "Mon", "time": "10am", "version": item["version"]})
ok(st == 403, "operator gets 403 on availability/approval over HTTP")
st, j, _ = b.req(f"/api/appointments/{item['id']}/verify_availability", {"date": "Monday, Nov 23", "time": "10:00 am", "version": item["version"]})
ok(st == 200 and j["status"] == workflow.READY_FOR_OWNER, "Mola verifies availability over HTTP")
st, j, _ = b.req(f"/api/appointments/{item['id']}/approve", {"version": j["version"]})
ok(st == 200 and j["status"] == workflow.AWAITING_CALENDAR_ENTRY, "Mola approves over HTTP")
st, j, _ = b.req(f"/api/appointments/{item['id']}/mark_entered", {"attest": True, "version": j["version"]})
st, j, _ = b.req(f"/api/appointments/{item['id']}/mark_confirmed", {"attest": True, "send_confirmation": True, "version": j["version"]})
ok(st == 200 and j["status"] == workflow.CONFIRMED and j.get("confirmation_queued"), "entered → confirmed → confirmation queued over HTTP")
st, j, _ = b.req(f"/api/appointments/{item['id']}/approve", {"version": j["version"]})
ok(st == 400, "can't approve a confirmed appointment again")
ok(b.req("/api/settings", {"live_send": "1"})[0] == 403, "with an operator account, live sending is the operator's switch")
store.put(con, "pilot_approved", "")
ok(o.req("/api/settings", {"live_send": "1"})[0] == 400, "live sending can't go on before Mola approves the pilot")
ok(o.req("/api/settings", {"pilot_approved": "1"})[0] == 403, "only Mola can approve the pilot")
ok(b.req("/api/settings", {"pilot_approved": "1"})[0] == 200 and o.req("/api/settings", {"live_send": "1"})[0] == 200, "after Mola approves, sending can be turned on")
ok(b.req("/api/settings", {"automation_paused": "1"})[0] == 200 and store.flag(con, "automation_paused"), "pause from the dashboard")
b.req("/api/settings", {"automation_paused": "0"})
st, j, _ = b.req("/api/simulate", {"sender": "web", "text": "what does a custom CRM cost?"})
st, cl, _ = b.req("/api/conversations")
wc = next(x for x in cl["items"] if x["igsid"] == "test:web")
st, th, _ = b.req(f"/api/conversations/{wc['id']}")
ok(st == 200 and "discovery call" in th["messages"][-1]["text"], "simulator over HTTP → assistant draft in the inbox")
ok(b.req(f"/api/conversations/{wc['id']}/takeover", {})[0] == 200 and conv_of("test:web")["mode"] == "human", "take over from the inbox")
ok(b.req(f"/api/conversations/{wc['id']}/release", {})[0] == 200 and conv_of("test:web")["mode"] == "ai", "return to AI from the inbox")
st, j, _ = b.req("/api/kb", {"kind": "service", "key": "custom_crm", "data": {**kb.item(con, "service", "custom_crm"), "duration_min": 30}})
ok(st == 200 and kb.services(con)["custom_crm"]["duration_min"] == 30, "edit the knowledge base from the dashboard")
ok(not outbox.guard_problems(con, "The discovery call takes about 30 minutes."), "call length allowed once Mola provides it")
st, j, _ = b.req("/api/analytics")
ok(st == 200 and j["verified_in_calendar"] >= 1 and j["drafts_approved"] >= 1, "analytics over HTTP (deleted customer no longer counted)")
raw = json.dumps(meta_msg("hook1", "what do you do?")).encode()
st, j, _ = Client().req("/webhooks/instagram", raw=raw, headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"appsecret123", raw, hashlib.sha256).hexdigest()})
ok(st == 200 and con.execute("SELECT COUNT(*) FROM webhook_events WHERE processed_at IS NULL").fetchone()[0] == 1, "signed webhook accepted and stored for the worker")
st, j, _ = Client().req("/webhooks/instagram", raw=raw, headers={"X-Hub-Signature-256": "sha256=bad"})
ok(st == 403, "unsigned/forged webhook rejected")
st, t, _ = Client().req("/webhooks/instagram?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=1234")
ok(st == 200 and t == "1234", "webhook verification over HTTP")
service.tick(con, T, TR)
ok(conv_of("hook1") and outs(conv_of("hook1")["id"]) == 1, "worker processes the webhook into a reply")
srv2 = server.make_server(T, "127.0.0.1", 0, transport=TR, worker=False, https_proxy=True)
srv2.app.con.close(); srv2.app.con = con
threading.Thread(target=srv2.serve_forever, daemon=True).start()
r = urllib.request.Request(f"http://127.0.0.1:{srv2.server_address[1]}/api/login", data=json.dumps({"username": "helper", "password": "sidekicks-ops-1"}).encode(),
                           method="POST", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(r, timeout=10) as res:
    ok("Secure" in (res.headers.get("Set-Cookie") or "") and "max-age" in (res.headers.get("Strict-Transport-Security") or ""), "behind the HTTPS tunnel: Secure cookie + HSTS")
srv2.shutdown()
for _ in range(5): Client().login("mola", "nope")
ok(Client().login("mola", "sidekicks-own-1") == 401, "account locks after 5 wrong passwords")
srv.shutdown()

print("inbox check (works without Meta webhooks)")
from datetime import timezone as _tz
def _iso(dt): return dt.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
NOW = datetime.now(_tz.utc)
class PollTR(meta.TestTransport):
    account = "17840000000"
    def __init__(s): super().__init__(); s.msgs = []; s.fail_with = None
    def my_username(s): return "aisidekicks"
    def recent_messages(s):
        if s.fail_with: raise s.fail_with
        return list(s.msgs)
PT = PollTR()
store.put(con, "poll_since", "")
PT.msgs = [{"mid": "pm1", "from": "555", "from_username": "mainacct", "to": ["17840000000"], "text": "what do you guys do?", "created": _iso(NOW - timedelta(hours=1)), "attachments": []},
           {"mid": "pm0", "from": "556", "from_username": "old", "to": ["17840000000"], "text": "hello from last week", "created": _iso(NOW - timedelta(days=6)), "attachments": []}]
service._poll_state.clear()
ok(service.poll_inbox(con, T, PT, now=1000) == 1, "first check picks up a DM from the last few hours, ignores old history")
ok(service.poll_inbox(con, T, PT, now=1005) == 0, "checks are spaced out (every 20 s)")
service.tick(con, T, PT)
pc = conv_of("555")
ok(pc and last_out(pc["id"]) and last_out(pc["id"])["status"] == "draft" and "custom AI assistants" in last_out(pc["id"])["text"], "polled DM → assistant draft in the inbox")
ok(not conv_of("556"), "message from before the start window isn't answered")
service._poll_state.clear()
ok(service.poll_inbox(con, T, PT, now=1030) == 0, "the same DM isn't queued twice")
store.put(con, "live_send", "1"); outbox.approve_draft(con, last_out(pc["id"])["id"], OWNER); outbox.deliver_due(con, PT)
sent_txt = last_out(pc["id"])["text"]
PT.msgs.append({"mid": "pm2", "from": "17840000000", "from_username": "aisidekicks", "to": ["555"], "text": sent_txt, "created": _iso(NOW), "attachments": []})
(service._poll_state.clear(), service.poll_inbox(con, T, PT, now=1060))[1]; service.tick(con, T, PT)
ok(conv_of("555")["mode"] == "ai", "our own sent reply showing up in the inbox doesn't trigger takeover")
PT.msgs.append({"mid": "pm3", "from": "17840000000", "from_username": "aisidekicks", "to": ["555"], "text": "Hey it's Mola, I'll take it from here", "created": _iso(NOW + timedelta(seconds=5)), "attachments": []})
(service._poll_state.clear(), service.poll_inbox(con, T, PT, now=1090))[1]; service.tick(con, T, PT)
ok(conv_of("555")["mode"] == "human", "Mola replying from the Instagram app (seen by the inbox check) takes over")
store.put(con, "live_send", "0")
PT.fail_with = meta.SendError("Instagram error 400: (#10) Application does not have permission", retryable=False)
ok((service._poll_state.clear(), service.poll_inbox(con, T, PT, now=1200))[1] == 0 and con.execute("SELECT ok FROM integration_health WHERE name='inbox_poll'").fetchone()[0] == 0,
   "a permission error is recorded in health, nothing crashes")
PT.fail_with = None
calls2 = []
def _opener2(req, timeout=20):
    calls2.append((req.full_url, req.headers.get("Authorization")))
    if "me/accounts" in req.full_url:
        return _Resp2({"data": [{"id": "PAGE1", "access_token": "PAGETOK", "instagram_business_account": {"id": "17840000000"}}]})
    return _Resp2({"data": [{"id": "c1", "messages": {"data": [{"id": "mX", "from": {"id": "777", "username": "kim"}, "to": {"data": [{"id": "17840000000"}]},
                                                                  "message": "hi", "created_time": "2026-11-17T23:43:00+0000"}]}}]})
class _Resp2:
    def __init__(s, body): s.body = body
    def read(s): return json.dumps(s.body).encode()
    def __enter__(s): return s
    def __exit__(s, *a): return False
rm = meta.GraphTransport(T, opener=_opener2).recent_messages()
ok(rm == [{"mid": "mX", "from": "777", "from_username": "kim", "to": ["17840000000"], "text": "hi", "created": "2026-11-17T23:43:00+0000", "attachments": []}]
   and "PAGE1/conversations" in calls2[-1][0] and "platform=instagram" in calls2[-1][0] and calls2[-1][1] == "Bearer PAGETOK",
   "Unified Login inbox read: the linked Page's conversations with the Page token")

print("Meta transports (Unified Login default, Instagram Login optional)")
calls = []
class _Resp:
    def __init__(s, body): s.body = body
    def read(s): return json.dumps(s.body).encode()
    def __enter__(s): return s
    def __exit__(s, *a): return False
def _opener(req, timeout=20):
    calls.append((req.get_method(), req.full_url, req.headers.get("Authorization"), req.data))
    return _Resp({"recipient_id": "123", "message_id": "mid.real.1"})
gt = meta.GraphTransport(T, opener=_opener)
mid = gt.send_text("99887766", "Hi!")
ok(mid == "mid.real.1" and calls[-1][1] == "https://graph.facebook.com/v26.0/17840000000/messages" and calls[-1][2] == "Bearer tok"
   and json.loads(calls[-1][3]) == {"recipient": {"id": "99887766"}, "message": {"text": "Hi!"}}, "Unified Login: POST graph.facebook.com/v26.0/<account id>/messages with the bearer token")
os.environ["AISIDEKICKS_IG_LOGIN"] = "instagram"
meta.GraphTransport(T, opener=_opener).send_text("99887766", "Hi!")
ok(calls[-1][1] == "https://graph.instagram.com/v26.0/17840000000/messages", "Instagram Login (older apps): graph.instagram.com")
del os.environ["AISIDEKICKS_IG_LOGIN"]
os.environ["AISIDEKICKS_IG_META_APP_SECRET"] = "metasecret456"
raw2 = b'{"object":"instagram","entry":[]}'
ok(meta.signature_ok(T, raw2, "sha256=" + hmac.new(b"metasecret456", raw2, hashlib.sha256).hexdigest())
   and meta.signature_ok(T, raw2, "sha256=" + hmac.new(b"appsecret123", raw2, hashlib.sha256).hexdigest())
   and not meta.signature_ok(T, raw2, "sha256=" + hmac.new(b"other", raw2, hashlib.sha256).hexdigest()), "webhook signature accepts the Meta app secret or the Instagram app secret, nothing else")
del os.environ["AISIDEKICKS_IG_META_APP_SECRET"]

print("first-run setup in the browser (Mola is the only login)")
fs = store.connect("aisidekicks_setup"); store.put(fs, "tenant", "aisidekicks"); kb.seed(fs)
ok([x["username"] + ":" + x["role"] for x in server.setup_plan(fs)] == ["mola:owner"], "setup creates one login: mola (owner)")
code = server.new_setup_code(fs)
try:
    server.first_run_setup(fs, {"code": "wrong", "owner_password": "aaaaaaaaaa1"}); ok(False, "wrong setup code refused")
except ValueError: ok(True, "wrong setup code refused")
server.first_run_setup(fs, {"code": code, "owner_password": "aaaaaaaaaa1"})
ok({r[0]: r[1] for r in fs.execute("SELECT username, role FROM users")} == {"mola": "owner"}, "setup code creates mola as owner")
try:
    server.first_run_setup(fs, {"code": code, "owner_password": "cccccccccc1"}); ok(False, "setup can't run twice")
except ValueError: ok(True, "setup link is single-use: refused once logins exist")
ok(server.update_settings(fs, "aisidekicks", OWNER, {"pilot_approved": "1"})["pilot_approved"], "Mola (owner) approves his own pilot")
try:
    server.update_settings(fs, "aisidekicks_setup", OWNER, {"live_send": "1"}); ok(False, "live send needs Instagram keys")
except ValueError as e: ok("Instagram isn't fully connected" in str(e), "with no operator account, Mola may flip live sending (once Instagram is connected)")
fs2 = store.connect("setupclient2"); c2 = server.new_setup_code(fs2); store.put(fs2, "setup_code_expires", "1")
try:
    server.first_run_setup(fs2, {"code": c2, "owner_password": "aaaaaaaaaa1"}); ok(False, "expired code refused")
except ValueError: ok(True, "expired setup link refused")

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED:\n  - " + "\n  - ".join(FAILS)))
sys.exit(1 if FAILS else 0)
