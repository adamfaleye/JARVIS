"""LinkedIn owner list: picks owners in the four niches from the CRM, drafts notes (AI or template), checks them, never sends,
moves the CRM stage only when the owner marks a reply. No network, no AI spend (the AI call is faked).
Run: python tests/test_linkedin.py"""
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, dcrm, linkedin, duties
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

con = hq.connect(); dc = dcrm.connect()
def biz(name, industry, services="", city="Boston", owner="", title="Owner"):
    rec = {"company": name, "industry": industry, "services": services, "city": city, "state": "MA", "phone": "617-555-%04d" % (abs(hash(name)) % 9999)}
    if owner:
        rec.update(contact_name=owner, contact_title=title)
    return dcrm.upsert(dc, rec, check_email_dns=False)[0]

hv = biz("Lakeview Mechanical", "home services", "HVAC, heating and cooling", owner="Ray Lakeview")
pl = biz("Quincy Plumbing & Drain", "home services", "plumbing, drain cleaning")
rf = biz("North Shore Roofing", "home services", "roofing, gutters", city="Salem")
pm = biz("Harbor Property Management", "property management", "", owner="Dana Cole", title="President")
au = biz("Speedy Auto Repair", "auto repair", "brakes")
el = biz("Sparks Electric", "home services", "electrical")
rp = biz("Replied Heating Co", "home services", "heating")
dcrm.move(dc, rp, "Replied", by="owner", note="called back")

print("Niches")
ok(linkedin.niche_of({"name": "Quincy Plumbing & Heating", "industry": "home services"}) == "plumbing", "plumbing & heating counts as plumbing")
ok(linkedin.niche_of({"name": "Lakeview Mechanical", "industry": "home services"}) == "hvac", "a mechanical contractor is HVAC")
ok(linkedin.niche_of({"name": "Harbor PM", "industry": "property management"}) == "real_estate", "property management is real estate")
ok(linkedin.niche_of({"name": "Speedy Auto", "industry": "auto repair"}) is None, "auto repair is outside the niches")
cands = linkedin.candidates(con, dc, 0)
ids = [c["business_id"] for c in cands]
ok(set(ids) == {hv, pl, rf, pm}, f"candidates are the four niche businesses only ({len(ids)})")
ok(rp not in ids, "a business already in a conversation isn't picked")
ok(ids[:2] and set(ids[:2]) == {hv, pm}, "owners we know by name come first")

print("Building the list with AI drafts (faked), checked")
calls = []
def fake_ask(con_, **kw):
    calls.append(kw)
    people = kw["data"]["people"]
    drafts = []
    for p in people:
        if p["company"].startswith("North Shore"):
            drafts.append({"key": p["key"], "text": "Hi, we're the #1 AI agency and guarantee 3x more jobs. Let's connect."})  # fails the claims check
        elif p["company"].startswith("Quincy"):
            drafts.append({"key": p["key"], "text": "x" * 400})  # too long
        else:
            drafts.append({"key": p["key"], "text": f"Hi {p['first_name'] or 'there'}, I build AI assistants for {p['niche']} businesses in {p['city']} that catch missed calls. Would be good to connect."})
    return {"drafts": drafts}, {"cost": 0}
r = linkedin.build(con, limit=20, ask=fake_ask)
ok(r["added"] == 4, f"all four owners added ({r})")
ok(len(calls) == 1 and calls[0]["agent"] == "sdr" and calls[0]["tier"] == "fast", "one cheap batch call on the SDR budget")
t = linkedin.today(con)
by = {i["business_id"]: i for i in t["items"]}
ok(by[hv]["note_by"] == "ai" and by[hv]["note"].startswith("Hi Ray"), "good AI note kept, addressed by first name")
ok(by[rf]["note_by"] == "template" and "#1" not in by[rf]["note"], "a note with unproven claims is replaced by the template")
ok(by[pl]["note_by"] == "template" and len(by[pl]["note"]) <= linkedin.NOTE_MAX, "a note over the limit is replaced")
ok(all(len(i["note"]) <= linkedin.NOTE_MAX for i in t["items"]), "every note fits LinkedIn's limit")
ok("keywords=Ray%20Lakeview%20Lakeview%20Mechanical" in by[hv]["link"], "link searches LinkedIn for the owner at that company")
ok(by[pl]["link"].endswith("keywords=Quincy%20Plumbing%20%26%20Drain"), "no owner name: the link searches the company")
ok(len(t["searches"]) == 4 and all(s["searches"] for s in t["searches"]), "searches for new owners in each niche")
ok(linkedin.build(con, limit=20, ask=fake_ask)["added"] == 0, "building again adds nothing new (CRM pool used up)")

print("AI unavailable -> templates still give a list")
biz("Revere HVAC Pros", "home services", "hvac installs", city="Revere")
def broken(con_, **kw):
    raise RuntimeError("cap reached")
r2 = linkedin.build(con, limit=20, ask=broken)
ok(r2["added"] == 1 and r2["ai_drafted"] == 0, "falls back to the template when the AI call fails")

print("Marking what the owner did")
ok(raises(lambda: linkedin.mark(con, hv, "messaged"), "can't go"), "can't skip straight from queued to messaged")
ok(raises(lambda: linkedin.mark(con, hv, None, profile_url="https://evil.example/in/x"), "profile link"), "only real LinkedIn profile links are accepted")
m = linkedin.mark(con, hv, "sent", profile_url="https://www.linkedin.com/in/ray-lakeview-123/?trk=abc")
ok(m["item"]["status"] == "sent" and m["item"]["profile_url"] == "https://www.linkedin.com/in/ray-lakeview-123/", "sent, profile link saved without tracking")
ok(m["item"]["link"] == m["item"]["profile_url"], "the link now opens the profile itself")
ok(any("connection request sent" in f["value"] for f in dcrm.detail(dc, hv)["facts"]), "the CRM record shows the request was sent")
m = linkedin.mark(con, hv, "accepted", ask=lambda c, **kw: ({"drafts": [{"key": hv, "text": "Thanks for connecting, Ray. When a call comes in after hours, who picks it up?"}]}, {}))
ok(m["item"]["followup_by"] == "ai" and "after hours" in m["item"]["followup"], "accepted: a first message is drafted")
ok(any(i["business_id"] == hv for i in linkedin.today(con, day="2099-01-01")["items"]), "accepted ones stay on the list until messaged")
linkedin.mark(con, hv, "messaged")
ok(any(i["business_id"] == hv for i in linkedin.today(con, day="2099-01-01")["waiting"]), "messaged ones wait under 'waiting on a reply' on later days")
before = dcrm.detail(dc, hv)["stage"]
linkedin.mark(con, hv, "replied", reply="Sure, send me something")
ok(dcrm.detail(dc, hv)["stage"] == "Replied" and before != "Replied", "a logged reply moves the CRM stage to Replied")
m = linkedin.mark(con, rf, "skipped")
ok(m["item"]["status"] == "skipped", "skip works")
ok(linkedin.mark(con, pl, None, note="Hi, short custom note.")["item"]["note_by"] == "owner", "the owner can edit a note")
ok(raises(lambda: linkedin.mark(con, pl, None, note="y" * 400), "300"), "an edited note over 300 characters is refused")

print("Carry-over: unsent ones count toward tomorrow's 20")
tom = linkedin.today(con, day="2099-01-01")
ok(any(i["business_id"] == pl for i in tom["items"]), "an unsent owner from today shows up tomorrow")

print("Adding an owner found on LinkedIn")
ok(raises(lambda: linkedin.add(con, "Acme", niche="dentist"), "niche"), "niche must be one of the four")
a = linkedin.add(con, "Bay State Roofing", owner="Mike Doyle", title="Owner", profile_url="https://www.linkedin.com/in/mike-doyle/", niche="roofing",
                 city="Lynn", ask=fake_ask)
ok(a["created"] and a["item"]["status"] == "queued" and a["item"]["profile_url"].endswith("/mike-doyle/"), "new owner added to the CRM and today's list")
d = dcrm.detail(dc, a["business_id"])
ok(any(c["name"] == "Mike Doyle" for c in d["contacts"]), "the owner is saved as the decision maker")
ok(linkedin.add(con, "Bay State Roofing", niche="roofing")["already_listed"], "adding the same company twice doesn't duplicate it")

print("Wiring")
ok("li_today" in hq.READ_OPS and {"li_build", "li_mark", "li_add"} <= hq.WRITE_OPS, "ops registered (read vs write)")
ok(hq.handle(con, {}, {"op": "li_today"})["daily"] == 20, "hq.li_today answers")
ok(any(x[0] == "sdr.linkedin" for x in duties.DUTIES), "the daily duty is on the SDR's board")

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED"))
sys.exit(1 if FAILS else 0)
