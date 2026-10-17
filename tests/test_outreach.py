"""Outreach tests with a fake Twilio client. Run: python -m tests.test_outreach  (throwaway database, nothing is sent)."""
import json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, crm, sms, outreach as O, hunter as H, agent

tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"
config.ROOT = tmp
(tmp / "data").mkdir()
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_MESSAGING_SERVICE_SID"):
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

A, B = "+17275550101", "+17275550102"
class Fake:
    def __init__(s):
        s.sent, s.calls_made, s.status, s.inbound, s.call_list, s.n = [], [], {}, [], [], 0
        s.messages = s; s.calls = NS(create=s._call, list=lambda **k: s.call_list)
        s.incoming_phone_numbers = NS(list=lambda **k: [NS(phone_number=A, sid="PN1", friendly_name="Tampa 1", capabilities={"sms": True, "voice": True}),
                                                        NS(phone_number=B, sid="PN2", friendly_name="Tampa 2", capabilities={"sms": True, "voice": True})],
                                      create=lambda phone_number: NS(phone_number=phone_number, sid="PN9"))
        s.balance = NS(fetch=lambda: NS(balance="12.34", currency="USD"))
        s.usage = NS(records=NS(this_month=NS(list=lambda category, limit: [NS(count="3", usage="3", usage_unit="messages", price="0.0237", price_unit="usd")])))
    def create(s, to, from_, body):
        s.n += 1; sid = f"SM{s.n}"; s.sent.append((to, from_, body)); return NS(sid=sid, status="queued")
    def list(s, **k):
        return s.inbound
    def __call__(s, sid):
        return NS(fetch=lambda: NS(status=s.status.get(sid, ("delivered", None))[0], error_code=s.status.get(sid, (None, None))[1]))
    def _call(s, to, from_, twiml):
        s.calls_made.append((to, from_, twiml)); return NS(sid="CA1")

cfg = {"timezone": "America/New_York", "sms": {"enabled": False, "require_consent": True, "quiet_hours": {"start": "09:00", "end": "20:00"},
       "daily_limit": 150, "dnc_file": "data/dnc.txt", "sender_name": "Ademola",
       "sequence": [{"day": 0, "text": "Hi {first}, it's {sender} about {street}. Reply STOP to opt out."}, {"day": 2, "text": "{first}, following up."}]}}
con = O.connect(); H.connect()
NOON = datetime(2026, 10, 15, 12, 0)
fk = Fake()

print("campaign rules")
ok(raises(lambda: O.save_campaign(con, cfg, {"name": "X", "kind": "blast", "steps": [{"text": "Hi {first}"}]}), "opt-out"), "first message needs STOP wording")
ok(raises(lambda: O.save_campaign(con, cfg, {"name": "X", "kind": "blast", "steps": [{"text": "Hi {owner} STOP"}]}), "unknown merge"), "unknown merge field rejected")
ok(raises(lambda: O.save_campaign(con, cfg, {"name": "X", "kind": "drip", "steps": [{"day": 0, "text": "a STOP"}, {"day": 0, "text": "b"}]}), "day after"), "drip days must increase")
ok(raises(lambda: O.save_campaign(con, cfg, {"name": "", "kind": "blast", "steps": [{"text": "STOP"}]}), "name"), "name required")
c = O.save_campaign(con, cfg, {"name": "Oct blast", "kind": "blast", "numbers": [A, B], "steps": [{"text": "Hi {first}, {sender} here. Cash offer on {street}? Reply STOP to opt out."}, {"text": "ignored"}]})
ok(c["status"] == "draft" and len(c["steps"]) == 1 and c["numbers"] == [A, B], "blast saved as draft, one message")
ok(O.segments("a" * 160) == 1 and O.segments("a" * 161) == 2 and O.segments("héllo ✓") == 1 and O.segments("✓" * 71) == 2, "segment counter (GSM and unicode)")

print("audience and consent")
hc = H.connect(); hc.execute("INSERT INTO contact_points(id,dedupe_key,property_id,phone,auto_approved,manual_allowed) VALUES('cp1','k1','p1','+18135550004',1,0)"); hc.commit(); hc.close()
con.execute("INSERT INTO messages(phone,direction,body,sid,at) VALUES('+18135550003','in','yes call me','SMin0','2026-10-01T10:00:00')"); con.commit()
r = O.add_audience(con, cfg, {"campaign_id": c["id"], "contacts": [
    {"phone": "813-555-0001", "name": "SMITH, JOHN", "address": "12 Oak St, Tampa", "consent": True},
    {"phone": "8135550002", "name": "Jane Doe"},                       # no consent
    {"phone": "8135550003", "name": "Texted First"},                   # texted us first
    {"phone": "8135550004", "name": "Hunter Approved"},                # approved in The Hunter
    {"phone": "123", "name": "Bad"},
    {"phone": "8135550001", "name": "dup"}]})
ok(r["added"] == 4 and r["invalid"] == 1 and r["duplicates"] == 1, "adds valid numbers once")
ok(r["eligible"] == 3 and list(r["reasons"].values()) == [1] and "consent" in list(r["reasons"])[0].lower(), "skip-traced number without consent is excluded")
e = con.execute("SELECT first FROM enrollments WHERE phone='+18135550001'").fetchone()
ok(e["first"] == "John", "first name parsed from 'SMITH, JOHN'")

print("launch gates")
ok(raises(lambda: O.launch(con, cfg, c["id"]), "texting is off"), "launch blocked while texting is off")
cfg["sms"]["enabled"] = True
ok(raises(lambda: O.launch(con, cfg, c["id"]), "twilio isn't set up"), "launch blocked without Twilio keys")
os.environ.update(TWILIO_ACCOUNT_SID="ACtest", TWILIO_AUTH_TOKEN="x", TWILIO_FROM=A)
O.owned_numbers(con, fk, refresh=True)
crm.put(con, "settings", {"id": "settings", "emergencyStop": True}); con.commit()
ok(raises(lambda: O.launch(con, cfg, c["id"]), "emergency"), "launch blocked by emergency stop")
crm.put(con, "settings", {"id": "settings", "emergencyStop": False}); con.commit()
L = O.launch(con, cfg, c["id"], now_dt=NOON)
ok(L["activated"] == 3 and L["blocked"] == 1, "launch activates eligible, blocks the rest")
ok(raises(lambda: O.launch(con, cfg, c["id"]), "already"), "can't launch twice")

print("sending")
ok(O.tick(con, cfg, client=fk, now_dt=NOON.replace(hour=21), pace=0) == 0, "nothing sent outside texting hours")
sms._add_dnc(cfg, "+18135550003")  # DNC added after launch
n = O.tick(con, cfg, client=fk, now_dt=NOON, pace=0)
ok(n == 2 and len(fk.sent) == 2, "sends to the 2 still-eligible people")
ok(con.execute("SELECT status FROM enrollments WHERE phone='+18135550003'").fetchone()["status"] == "stopped", "DNC added later is honoured at send time")
ok(fk.sent[0][2].startswith("Hi John, Ademola here. Cash offer on 12 Oak St?"), "merge fields rendered")
ok({x[1] for x in fk.sent} == {A, B}, "spreads across the campaign's numbers")
ok(O.list_campaigns(con, cfg)[0]["status"] == "done", "blast finishes when everyone is done")
ok(O.tick(con, cfg, client=fk, now_dt=NOON, pace=0) == 0, "no duplicate sends")

print("drip")
d = O.save_campaign(con, cfg, {"name": "Drip", "kind": "drip", "steps": [{"day": 0, "text": "Hi {first} STOP to opt out"}, {"day": 3, "text": "Day 3 {first}"}]})
O.add_audience(con, cfg, {"campaign_id": d["id"], "contacts": [{"phone": "8135550001", "name": "John Smith"}, {"phone": "8135550009", "name": "Ann Lee"}], "attest_consent": True})
O.launch(con, cfg, d["id"], now_dt=NOON)
before = len(fk.sent)
O.tick(con, cfg, client=fk, now_dt=NOON, pace=0)
ok(len(fk.sent) - before == 1, "20-hour gap: John (texted today by the blast) waits, Ann gets message 1")
sticky = con.execute("SELECT sender FROM sender_map WHERE phone='+18135550001'").fetchone()["sender"]
O.tick(con, cfg, client=fk, now_dt=NOON + timedelta(days=1), pace=0)
ok(fk.sent[-1][0] == "+18135550001" and fk.sent[-1][1] == sticky, "John later gets message 1 from the same number as before")
O.on_inbound(con, "+18135550009", A)
O.tick(con, cfg, client=fk, now_dt=NOON + timedelta(days=4), pace=0)
ok(not any(x[0] == "+18135550009" and "Day 3" in x[2] for x in fk.sent), "a reply stops the drip")
ok(any(x[0] == "+18135550001" and x[2] == "Day 3 John" for x in fk.sent), "drip step 2 goes out on schedule")
O.on_inbound(con, "+18135550001", A, stop=True)
ok(con.execute("SELECT source FROM dnc_meta WHERE phone='+18135550001'").fetchone()["source"] == "stop", "STOP lands on Do Not Contact")

print("limits")
cfg["sms"]["daily_limit"] = len(fk.sent) - 0  # already at today's cap
b2 = O.save_campaign(con, cfg, {"name": "Cap", "kind": "blast", "steps": [{"text": "x STOP"}]})
O.add_audience(con, cfg, {"campaign_id": b2["id"], "contacts": [{"phone": "8135550020"}], "attest_consent": True}); O.launch(con, cfg, b2["id"], now_dt=NOON)
ok(O.tick(con, cfg, client=fk, now_dt=NOON + timedelta(days=4), pace=0) == 1, "daily limit counts per day")
cfg["sms"]["daily_limit"] = 150
O.set_settings(con, {"perNumberDaily": 1})
b3 = O.save_campaign(con, cfg, {"name": "Per", "kind": "blast", "numbers": [A], "steps": [{"text": "y STOP"}]})
O.add_audience(con, cfg, {"campaign_id": b3["id"], "contacts": [{"phone": "8135550031"}, {"phone": "8135550032"}], "attest_consent": True}); O.launch(con, cfg, b3["id"], now_dt=NOON)
ok(O.tick(con, cfg, client=fk, now_dt=NOON + timedelta(days=5), pace=0) == 1, "per-number daily limit holds")
O.set_settings(con, {"perNumberDaily": 100})
ok(raises(lambda: O.set_settings(con, {"callEnd": "22:00"}), "between"), "calling hours can't go past 9 PM")

print("pause / cancel")
ok(O.set_status(con, b3["id"], "paused")["status"] == "paused" and O.tick(con, cfg, client=fk, now_dt=NOON + timedelta(days=6), pace=0) == 0, "paused campaign doesn't send")
O.set_status(con, b3["id"], "cancelled")
ok(raises(lambda: O.set_status(con, b3["id"], "running"), "can't"), "cancelled can't be resumed")
ok(O.duplicate(con, b3["id"])["id"].startswith("cmp_"), "duplicate makes a new draft")

print("DNC list")
O.dnc_add(con, cfg, "(813) 555-0077", "manual", "asked on a call")
items = {i["phone"]: i for i in O.dnc_list(con, cfg)}
ok(items["+18135550077"]["removable"] and not items["+18135550001"]["removable"], "only hand-added entries are removable")
ok(raises(lambda: O.dnc_remove(con, cfg, "+18135550001"), "START"), "STOP entry can't be removed")
O.dnc_remove(con, cfg, "+18135550077")
ok("+18135550077" not in O.dnc_set(con, cfg), "manual entry removed")

print("polling and delivery")
os.environ["TWILIO_FROM"] = A
fk.inbound = [NS(from_="+18135550055", to=B, body="Who is this?", sid="SMin1", direction="inbound", date_sent=datetime(2026, 10, 15, 13)),
              NS(from_=A, to="+18135550055", body="out", sid="SMx", direction="outbound-api", date_sent=datetime(2026, 10, 15, 13))]
agent._ensure_tables(con)
_on = agent.on_inbound; agent.on_inbound = lambda *a, **k: None
sms.poll(cfg, log=lambda *a: None, client=fk)
agent.on_inbound = _on
ok(con.execute("SELECT sender FROM sender_map WHERE phone='+18135550055'").fetchone()["sender"] == B, "reply on number 2 maps the person to number 2")
first_out = con.execute("SELECT sid FROM messages WHERE direction='out' ORDER BY id LIMIT 2").fetchall()
fk.status = {first_out[0]["sid"]: ("delivered", None), first_out[1]["sid"]: ("undelivered", "30007")}
con.execute("UPDATE messages SET status='sent', at=? WHERE sid IN (?,?)", (datetime.now().isoformat(timespec="seconds"), first_out[0]["sid"], first_out[1]["sid"])); con.commit()
O.refresh_status(con, cfg, fk)
st = {r["sid"]: (r["status"], r["error_code"]) for r in con.execute("SELECT sid,status,error_code FROM messages WHERE sid IN (?,?)", (first_out[0]["sid"], first_out[1]["sid"]))}
ok(st[first_out[0]["sid"]][0] == "delivered" and st[first_out[1]["sid"]] == ("undelivered", "30007"), "delivery receipts and carrier filtering recorded")
N = O.numbers(con, cfg, client=fk)
ok(len(N["numbers"]) == 2 and any(n["filtered_7d"] for n in N["numbers"]), "number health shows carrier-filtered texts")

print("inbox reply uses the person's number")
crm.put(con, "outbox", {"id": "o1", "phone": "+18135550055", "body": "Hey, it's Ademola. I buy houses around Tampa.", "status": "queued"}); con.commit()
agent.process_outbox(con, cfg, log=lambda *a: None, client=fk, now=NOON.replace(tzinfo=None))
ok(fk.sent[-1][0] == "+18135550055" and fk.sent[-1][1] == B, "Inbox reply goes out from the number they texted")

print("dialer")
ok(raises(lambda: O.place_call(con, cfg, {"phone": "8135550100"}, client=fk, now_dt=NOON), "cell number"), "needs your cell number")
O.set_settings(con, {"myCell": "774-444-0000"})
ok(raises(lambda: O.place_call(con, cfg, {"phone": "8135550001"}, client=fk, now_dt=NOON), "do not contact"), "won't dial Do Not Contact")
ok(raises(lambda: O.place_call(con, cfg, {"phone": "8135550100"}, client=fk, now_dt=NOON.replace(hour=21)), "calling hours"), "won't dial outside calling hours")
ok(raises(lambda: O.place_call(con, cfg, {"phone": "8135550004"}, client=fk, now_dt=NOON), "ok to call manually"), "Hunter number needs 'OK to call manually'")
r = O.place_call(con, cfg, {"phone": "8135550055", "name": "Lead"}, client=fk, now_dt=NOON)
to, frm, twiml = fk.calls_made[-1]
ok(to == "+17744440000" and frm == B and "<Number>+18135550055</Number>" in twiml and f'callerId="{B}"' in twiml, "rings your cell, then bridges the lead from their number")
fk.call_list = [NS(sid="CA1", parent_call_sid=None, direction="outbound-api", from_=B, to="+17744440000", status="completed", duration="95", start_time=datetime(2026, 10, 15, 12)),
                NS(sid="CA2", parent_call_sid="CA1", direction="outbound-dial", from_=B, to="+18135550055", status="completed", duration="80", start_time=datetime(2026, 10, 15, 12)),
                NS(sid="CA3", parent_call_sid=None, direction="inbound", from_="+18135559999", to=A, status="no-answer", duration="0", start_time=datetime(2026, 10, 15, 11))]
cl = O.call_log(con, cfg, client=fk)["calls"]
ok(len(cl) == 2 and cl[0]["type"] == "dialer" and cl[0]["duration"] == 80 and cl[1]["missed"], "call log merges dialer legs and flags missed calls")

print("usage, numbers, legacy, bridge")
u = O.usage(con, client=fk)
ok(u["balance"] == 12.34 and u["items"], "Twilio balance and this month's usage")
ok(O.buy_number(con, {"phone": "+18135551212"}, client=fk)["sid"] == "PN9", "buying a number returns its id")
con.execute("INSERT INTO contacts(phone,first_name,full_name,address,consent,enrolled_at,step,next_send,status) VALUES('+13305550001','Bo','Bo Ray','1 Elm',1,'2026-10-01T09:00:00',1,'2026-10-03T09:00:00','active')")
db.kv_set(con, "out_migrated", "")
con.execute("DELETE FROM kv WHERE k='out_migrated'"); con.commit()
ok(O.legacy_migrate(con, cfg) == 1 and con.execute("SELECT step FROM enrollments WHERE campaign_id='cmp_followup'").fetchone()["step"] == 1, "old config.yaml sequence moves into a drip campaign")
ov = O.overview(con, cfg)
ok(ov["sent_today"] >= 0 and len(ov["hours"]) == 24 and "temperature" in ov, "dashboard overview")
import yaml, mcp_server
(tmp / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
ok("isn't allowed" in mcp_server._hunter({"op": "out.launch", "id": "x"}, write=False)["error"], "bridge: writes can't go through pull")
_br = mcp_server._hunter({"op": "out.campaigns"}, write=False); ok(_br.get("ok"), "bridge: reads work through pull")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
