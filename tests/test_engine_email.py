"""Engine email: the AI Sidekicks signature, the campaign scoreboard, and sending/reading Gmail from the engine (Gmail faked, no network).
Run: python tests/test_engine_email.py"""
import email, json, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, hq, agents_ai, gmail
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
cfg = {"timezone": "America/New_York", "outreach_window": {"days": [0, 1, 2, 3, 4, 5, 6], "start": 0, "end": 24}}
con = hq.connect()
hq.set_email_footer(con, "Ademola Faleye, Demigod Intelligence · 1 Example St, Springfield, MA 01103")

print("Signature")
sig = hq.di_signature(con)
ok(sig == {"name": "Ademola Faleye", "company": "AI Sidekicks", "tagline": "AI Assistants That Answer Every Call, Text & Follow-Up", "address": "1 Example St, Springfield, MA 01103"}, "the signature: name, company, tagline, mailing address")
t = hq.create_task(con, "sdr.send_email", {"to": "a@b.com", "subject": "Hello", "body": "Hi there,\n\nShort note.\n\nIf not, reply 'no thanks'.\n\nBest,\nAdemola Faleye\nAI Sidekicks"}, division="demigod")
ok(t["params"]["body"].endswith("reply 'no thanks'.") and t["params"]["signature"]["tagline"], "a sign-off the writer added is removed and the official signature is attached instead (no duplicate)")
text, html = hq.render_email(t["params"])
ok(text.endswith("Ademola Faleye\nAI Sidekicks\nAI Assistants That Answer Every Call, Text & Follow-Up\n1 Example St, Springfield, MA 01103") and text.count("AI Sidekicks") == 1 and "Demigod" not in text, "plain-text version ends with the signature once, no separate footer line")
ok('src="cid:aisidekicks-logo"' in html and 'alt="AI Sidekicks"' in html and "AI Assistants That Answer Every Call, Text &amp; Follow-Up" in html and "<p" in html, "HTML version: paragraphs, signature, and the logo with alt text")
msg = gmail.build_message("a@b.com", "Hello", text, html)
raw = email.message_from_bytes(msg.as_bytes())
parts = [p.get_content_type() for p in raw.walk()]
ok("text/plain" in parts and "text/html" in parts and "image/png" in parts and gmail.LOGO.exists(), f"the email carries a plain-text fallback, the HTML, and the official logo inline ({parts})")
img = next(p for p in raw.walk() if p.get_content_type() == "image/png")
ok(img["Content-ID"] == "<aisidekicks-logo>" and len(img.get_payload(decode=True)) == gmail.LOGO.stat().st_size, "the logo is the official file, embedded (not a local path)")
old = email.message_from_bytes(gmail.build_message("a@b.com", "Hello", text, html.replace("cid:aisidekicks-logo", "cid:demigod-logo")).as_bytes())
ok(any(p.get("Content-ID") == "<demigod-logo>" for p in old.walk()), "an email approved before the rename still carries the logo")
ok("aisidekicks.local" in raw["Message-ID"], "the message id uses the new name")

print("Engine sender (fake Gmail)")
sent, threads = [], {}
def fake_send(to, subject, text, html=None, thread_id=None):
    sent.append({"to": to, "subject": subject, "text": text, "html": html, "thread": thread_id})
    tid = thread_id or f"th{len(sent)}"
    threads.setdefault(tid, []).append({"id": f"m{len(sent)}", "from": "Ademola Faleye <owner@example.com>", "internalDate": str(int(time.time() * 1000)), "text": text})
    return {"id": f"m{len(sent)}", "threadId": tid}
gmail.send = fake_send
gmail.thread = lambda tid: threads.get(tid, [])
gmail.profile = lambda: {"emailAddress": "owner@example.com"}
(tmp / "data" / "google_client.json").write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
ok(not gmail.configured(), "not configured until you sign in (token file)")
(tmp / "data" / "gmail_token.json").write_text(json.dumps({"refresh_token": "r"}))
db.kv_set(con, "hq_last_tick", hq.datetime.now().isoformat(timespec="seconds")); con.commit()
st = hq.engine_sender(con)
ok(st["configured"] and st["active"], "with Gmail connected and the loop running, the engine is the sender")
pros = [{"prospect_id": f"p{n}", "company": f"Co {n}", "to": f"owner@co{n}.com"} for n in range(3)]
c = hq.campaign_create(con, "Engine test", 20, hq.email_footer(con), pros)
class Fake:
    def __init__(s): s.messages = s
    def create(s, **kw):
        txt = json.dumps(kw["messages"]); co = next(f"Co {n}" for n in range(3) if f"Co {n}" in txt)
        body = json.dumps({"summary": "ok", "deliverable": "x" * 90, "needs_info": [], "subject": f"Time back for {co}", "body": f"Hi {co} team,\n\nAI Sidekicks builds custom AI assistants that work alongside a team and give it time back.\n\nIf it's not relevant, just reply 'no thanks' and I won't follow up."})
        return NS(content=[NS(type="text", text=body, citations=[])], stop_reason="end_turn", usage=NS(input_tokens=900, output_tokens=300, server_tool_use=NS(web_search_requests=0)))
agents_ai.CLIENT = Fake()
hq.run_due(con, cfg, log=lambda *a: None)
g = hq.campaign_get(con, c["id"], cfg)
ok(g["counts"] == {"ready": 3}, "the batch is drafted")
ok(all(i["subject"] == f"Custom AI Assistants & CRMs for {i['company']}" for i in g["items"]), "every email gets the house subject line, whatever the writer suggested")
ok(all("give them their time back" in i["body"] and "give it time back" not in i["body"] for i in g["items"]) and "give them their time back" in agents_ai.SPECS["sdr.campaign_email"]["instructions"], "the intro line reads 'work alongside a team and give them their time back'")
call_ok = "Make the business benefit the conversation" in json.dumps(agents_ai.SPECS["sdr.campaign_email"]) and "don't sign off" in agents_ai.SPECS["sdr.campaign_email"]["instructions"]
ok(call_ok, "the writer follows the AI Sidekicks email standard and leaves the signature to JARVIS")
r = hq.engine_test_send(con, c["id"], cfg)
ok(r["to"] == "owner@example.com" and sent[-1]["subject"].startswith("TEST: ") and "1 Example St" in sent[-1]["text"], "Send me a test goes only to your own Gmail address, marked TEST, with the signature")
ok(not hq.campaign_due(con, cfg)["due"], "a test send doesn't approve or queue anything")
hq.campaign_approve(con, c["id"], g["items_hash"], cfg)
n0 = len(sent)
hq.engine_send_tick(con, cfg, log=lambda *a: None)
ok(len(sent) == n0 + 1 and sent[-1]["to"] == "owner@co0.com" and "cid:aisidekicks-logo" in sent[-1]["html"], "the engine sends one approved email, HTML with the logo")
hq.engine_send_tick(con, cfg, log=lambda *a: None)
ok(len(sent) == n0 + 1, "the next waits a few minutes (paced)")
ok(hq.campaign_due(con, cfg)["engine_sender"], "the app is told the engine is sending, so it doesn't send too")
db.kv_set(con, "di_engine_next_send", "0"); con.commit()
hq.engine_send_tick(con, cfg, log=lambda *a: None)
ok(len(sent) == n0 + 2, "then the next goes out")
def boom(*a, **k): raise RuntimeError("quota exceeded")
gmail.send, real = boom, gmail.send
db.kv_set(con, "di_engine_next_send", "0"); con.commit()
hq.engine_send_tick(con, cfg, log=lambda *a: None)
ok(hq.engine_sender(con)["last_error"].startswith("quota") and hq.campaign_get(con, c["id"], cfg)["counts"].get("sent") == 2, "a Gmail error is recorded and the email isn't counted as sent")
gmail.send = real

print("Replies and the scoreboard")
tid = next(e["thread_id"] for e in hq.email_history(con)["sent"] if e["to"] == "owner@co0.com")
threads[tid].append({"id": "r1", "from": "Owner <Owner@Co0.com>", "internalDate": str(int(time.time() * 1000) + 5000), "text": "Sounds interesting, let's talk Tuesday."})
threads[tid].append({"id": "r2", "from": "stranger@x.com", "internalDate": str(int(time.time() * 1000) + 6000), "text": "spam"})
r = hq.engine_reply_tick(con, cfg, log=lambda *a: None)
ok(r["found"] == 1, "the engine reads replies from Gmail itself (only from the person emailed)")
ok(hq.engine_reply_tick(con, cfg, log=lambda *a: None) is None, "and checks again 10 minutes later, not every loop")
calls = {"n": 0}
def quota(*a, **k):
    calls["n"] += 1; raise RuntimeError("Gmail error 403: Quota exceeded for quota metric 'Total Query Cost'")
real_thread = gmail.thread; gmail.thread = quota
db.kv_set(con, "di_engine_next_reply", "0"); con.commit()
r = hq.engine_reply_tick(con, cfg, log=lambda *a: None, pause_s=0, sleep=lambda s: None)
ok(calls["n"] == 2 * r["checked"] and r["failed"] == r["checked"] and "Quota" in hq.engine_sender(con)["last_error"],
   f"each rate-limited thread is retried once (not hammered), and a round that mostly failed shows the error ({calls['n']} calls, {r['checked']} threads)")
flaky = {"n": 0}
def sometimes(tid):
    flaky["n"] += 1
    if flaky["n"] == 1:
        raise RuntimeError("Gmail error 403: Quota exceeded")
    return real_thread(tid)
gmail.thread = sometimes
db.kv_set(con, "di_engine_next_reply", "0"); con.commit()
r = hq.engine_reply_tick(con, cfg, log=lambda *a: None, pause_s=0, sleep=lambda s: None)
ok(r["failed"] == 0 and hq.engine_sender(con)["last_error"] == "", "a one-off rate limit is retried and succeeds, and the old error clears")
ok(hq.engine_sender(con)["reply_check"]["checked"] == r["checked"], "the last reply check (when, how many threads, failures) is reported")
gmail.thread = real_thread
row = con.execute("SELECT id FROM hq_email_replies").fetchone()
con.execute("UPDATE hq_email_replies SET class='meeting' WHERE id=?", (row["id"],)); con.commit()
hq.email_dnc_add(con, "owner@co1.com")
s = hq.campaign_stats(con, c["id"])
ok(s["sent"] == 2 and s["replied"] == 1 and s["wants_call"] == 1 and s["opted_out"] == 1 and s["reply_rate"] == 50.0, f"scoreboard: sent, replied, wants a call, opted out, reply rate ({s})")
mt = hq.create_task(con, "ae.book_meeting", {"attendee": "owner@co0.com", "start": "2026-11-10T14:00", "end": "2026-11-10T14:30", "title": "Call", "prospect_id": "p0"}, division="demigod")
hq.approve(con, mt["id"], mt["params_hash"]); hq.action_done(con, mt["id"], {"event_id": "ev1"})
ok(hq.campaign_stats(con, c["id"])["booked_calls"] == 1 and hq.campaigns(con)["campaigns"][0]["stats"]["booked_calls"] == 1, "booked calls count on the scoreboard and the campaign list")
ok(hq.campaign_get(con, c["id"], cfg)["campaign"]["daily_limit"] == 20 and hq.handle(con, cfg, {"op": "campaign_set", "id": c["id"], "action": "limit", "daily_limit": 500})["campaign"]["daily_limit"] == 500 and raises(lambda: hq.campaign_set(con, c["id"], "limit", 501), "500"), "the daily cap can be lifted up to Gmail's own 500")
nc = hq.campaign_create(con, "No cap", None, hq.email_footer(con), [])
ok(hq.campaign_get(con, nc["id"], cfg)["campaign"]["daily_limit"] == 500, "new campaigns have no JARVIS cap by default (Gmail's 500)")
hq.campaign_set(con, nc["id"], "cancel")
ok(hq.set_engine_send(con, False)["active"] is False and not hq.campaign_due(con, cfg)["engine_sender"], "you can turn engine sending off (the app sends instead)")
print("Sign-in (browser flow, Google faked)")
import importlib, re as _re, threading as _th, urllib.request as _ur, urllib.parse as _up
importlib.reload(gmail)
(tmp / "data" / "gmail_token.json").unlink()
posted = []
gmail._post = lambda url, data: posted.append(data) or {"refresh_token": "rt-123", "access_token": "at", "expires_in": 3600, "scope": gmail.SCOPES}
gmail._api = lambda method, path, body=None: {"emailAddress": "owner@example.com"}
seen = []
def fake_browser(msg):
    url = _re.search(r"https://\S+", msg).group(0); seen.append(url)
    q = _up.parse_qs(_up.urlparse(url).query)
    def hit():
        time.sleep(0.3); _ur.urlopen(q["redirect_uri"][0] + "/?" + _up.urlencode({"code": "c-1", "state": q["state"][0]})).read()
    _th.Thread(target=hit, daemon=True).start()
p = gmail.authorize(open_browser=False, log=fake_browser)
q = _up.parse_qs(_up.urlparse(seen[0]).query)
ok(q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and "gmail.send" in q["scope"][0] and q["redirect_uri"][0].startswith("http://127.0.0.1:"), "sign-in uses Google's browser flow with PKCE on this PC only")
ok(p["emailAddress"] == "owner@example.com" and json.loads((tmp / "data" / "gmail_token.json").read_text())["refresh_token"] == "rt-123" and posted[0]["code"] == "c-1", "the one-time code becomes a saved refresh token; no password is ever handled")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
