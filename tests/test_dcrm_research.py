"""AI Sidekicks CRM research: the scorer ignores negated evidence ("no booking software found"), a free-mail address on the
business's own site counts as published, and SDR email lookups / research queued for a CRM business save onto that record
by themselves (and can be queued in bulk). No network, no AI spend.
Run: python tests/test_dcrm_research.py"""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, dcrm, agents_ai
FAILS = []
def raises(fn, text=""):
    try:
        fn()
    except ValueError as e:
        return text.lower() in str(e).lower()
    return False
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

def dns(url):
    dom = url.split("name=")[1].split("&")[0]
    return json.dumps({"Status": 0, "Answer": [{"type": 15, "data": "10 mx." + dom}]})

con = hq.connect(); dc = dcrm.connect()
parts = lambda bid: dcrm.detail(dc, bid)["score_detail"]["parts"]

print("Negated evidence doesn't count")
ok([m.group(0) for m in dcrm.hits(dcrm.INQUIRY, "No booking or scheduling software found.")] == [], "'no booking or scheduling software found' is not a booking signal")
ok([m.group(0).lower() for m in dcrm.hits(dcrm.INQUIRY, "Customers book online. No CRM found.")] == ["book"], "'customers book online' still counts")
ok(len(dcrm.hits(dcrm.INQUIRY, "No software found, but they offer free estimates and 24/7 emergency calls")) == 3, "'no X, but they offer free estimates' counts what follows 'but'")
ok(len(dcrm.hits(dcrm.INQUIRY, "We don't take appointments online")) == 0, "a contraction (don't) negates too")
j, _, _ = dcrm.upsert(dc, {"company": "Sparks Electric", "phone": "781-555-0101", "city": "Wilmington", "industry": "home services",
                           "notes": "No booking or scheduling software found. Hours Mon-Fri 8-5.", "source": "https://sparks.example/about"}, fetch=dns)
ok(parts(j)["inquiries"]["points"] == 0, f"a record whose only 'booking' word is negated scores 0 for inquiries ({parts(j)['inquiries']['why']})")
dcrm.add_fact(dc, j, "note", "Free estimates; customers book a service appointment by phone.", "public", "https://angi.example/sparks")
dc.commit(); dcrm.score(dc, j)
ok(parts(j)["inquiries"]["points"] > 0, "real evidence still scores")
k, _, _ = dcrm.upsert(dc, {"company": "Tiny Co", "notes": "No employees listed anywhere; 3 technicians per the BBB.", "source": "https://bbb.example/tiny"}, fetch=dns)
ok("3 technicians" in parts(k)["size"]["why"], "a negated head-count is skipped and the real one used")

print("Free-mail address on their own site")
f1, _, _ = dcrm.upsert(dc, {"company": "Oak Electric", "website": "https://www.oakelectric.example", "email": "oakelectric@verizon.net",
                            "email_source": "https://www.oakelectric.example/contact.htm", "phone": "781-555-0102"}, fetch=dns)
c1 = dcrm.detail(dc, f1)["contacts"][0]
ok(c1["email_class"] == "public_unverified" and "own site" in c1["verify_note"], f"a verizon.net address published on their own site counts as published ({c1['verify_note']})")
f2, _, _ = dcrm.upsert(dc, {"company": "Elm Plumbing", "website": "https://elmplumbing.example", "email": "elmplumb@gmail.com",
                            "email_source": "https://www.yelp.com/biz/elm", "phone": "781-555-0103"}, fetch=dns)
ok(dcrm.detail(dc, f2)["contacts"][0]["email_class"] == "third_party", "the same kind of address on Yelp stays third-party")
dc.execute("UPDATE dc_contacts SET email_class='third_party' WHERE business_id=?", (f1,)); dc.commit(); dcrm.score(dc, f1)
low = parts(f1)["contact"]["points"]
dcrm.reclass_contacts(dc)
ok(dcrm.detail(dc, f1)["contacts"][0]["email_class"] == "public_unverified" and parts(f1)["contact"]["points"] > low,
   "records saved before this fix are upgraded on the next sync, and rescored")
ok(dcrm.detail(dc, f2)["contacts"][0]["email_class"] == "third_party", "...and the Yelp one isn't")

print("SDR lookups save onto the CRM record")
b, _, _ = dcrm.upsert(dc, {"company": "Lakeview Test Mechanical", "phone": "508-555-0104", "city": "Millbury", "industry": "home services",
                           "source": "https://www.yelp.com/biz/lakeview-test"}, fetch=dns)
r = dcrm.apply_sdr_result(dc, b, "sdr.find_contact", {"email": "csr@lakeviewtest.example", "email_source": "https://www.lakeviewtest.example/contact-us/",
                                                       "contact_page": "https://www.lakeviewtest.example/contact-us/", "website": "https://www.lakeviewtest.example/"}, fetch=dns)
d = dcrm.detail(dc, b)
ok(d["website"] == "https://www.lakeviewtest.example/" and d["domain"] == "lakeviewtest.example" and d["contact_page"].endswith("/contact-us/"), "website and contact page filled in")
ok(d["contacts"][0]["email"] == "csr@lakeviewtest.example" and d["contacts"][0]["email_class"] == "public_unverified" and "email" in r["saved"],
   "the email is saved with its source page and the same free checks as every other email")
r2 = dcrm.apply_sdr_result(dc, b, "sdr.research_prospect", {
    "facts": [{"fact": "Customers call or book online for appointments.", "source": "https://www.lakeviewtest.example/plumbing/"},
              {"fact": "BBB lists 70 employees.", "source": "https://www.bbb.example/lakeview"},
              {"fact": "No public software or scheduling platform found.", "source": "none found"}],
    "decision_maker": "Ray Lakeview Jr. (owner)", "software": "None found publicly"})
d = dcrm.detail(dc, b)
facts = [f for f in d["facts"] if f["field"] == "research"]
ok(len(facts) == 3 and facts[0]["source_url"].startswith("http") and any(f["grade"] == "third_party" and not f["source_url"] for f in facts),
   "each research fact is saved with its page; one without a real page is graded third-party")
dm = [c for c in d["contacts"] if c["role"] == "decision_maker"]
ok(len(dm) == 1 and dm[0]["name"] == "Ray Lakeview Jr." and dm[0]["title"].startswith("owner"), f"decision maker parsed: {dm and (dm[0]['name'], dm[0]['title'])}")
ok(not any(f["field"] == "software" for f in d["facts"]), "'None found' is not recorded as their software")
ok(d["priority"] == "High" and r2["score"] == d["score"] and d["score_detail"]["parts"]["size"]["points"] == 15, f"rescored on the new evidence: {d['score']} {d['priority']}")
dcrm.apply_sdr_result(dc, b, "sdr.research_prospect", {"facts": [], "decision_maker": "Ray Lakeview Jr. (owner)"})
ok(len([c for c in dcrm.detail(dc, b)["contacts"] if c["role"] == "decision_maker"]) == 1, "the same decision maker isn't added twice")
n, _, _ = dcrm.upsert(dc, {"company": "No Name Roofing", "phone": "413-555-0105"}, fetch=dns)
dcrm.apply_sdr_result(dc, n, "sdr.research_prospect", {"facts": [], "decision_maker": "Not found publicly"})
dcrm.apply_sdr_result(dc, n, "sdr.find_contact", {"email": "", "contact_page": ""})
dn = dcrm.detail(dc, n)
ok(not dn["contacts"] and any(f["field"] == "research_note" for f in dn["facts"]), "'not found' adds no contact; no email found is noted")

print("Wired into the SDR's tasks")
calls = []
def fake(kind, output):
    def run(con_, cfg, t):
        calls.append(t["id"])
        return {"verified": True, "summary": "found", "result": {"output": output, "meta": {}}, "note": "fake"}
    return run
saved = dict(agents_ai.EXECUTORS)
try:
    agents_ai.EXECUTORS["sdr.find_contact"] = fake("sdr.find_contact", {"email": "office@birchhvac.example", "email_source": "https://birchhvac.example/contact", "website": "https://birchhvac.example"})
    agents_ai.EXECUTORS["sdr.research_prospect"] = fake("sdr.research_prospect", {"facts": [{"fact": "Offers 24/7 emergency service and free estimates.", "source": "https://birchhvac.example/"}], "decision_maker": "Dana Birch, Owner"})
    fc, rp = hq._save_to_crm("sdr.find_contact"), hq._save_to_crm("sdr.research_prospect")
    bb, _, _ = dcrm.upsert(dc, {"company": "Birch HVAC", "phone": "617-555-0106", "city": "Lynn", "industry": "home services"}, fetch=dns)
    lowscore = dcrm.detail(dc, bb)["score"]
    t1 = hq.create_task(con, "sdr.find_contact", {"company": "Birch HVAC", "dc_business_id": bb}, division="demigod")
    t2 = hq.create_task(con, "sdr.research_prospect", {"company": "Birch HVAC", "dc_business_id": bb}, division="demigod")
    o1 = fc(con, {}, hq.get_task(con, t1["id"], events=False)); o2 = rp(con, {}, hq.get_task(con, t2["id"], events=False))
    db2 = dcrm.detail(dcrm.connect(), bb)
    ok(o1["verified"] and "saved to the CRM" in o1["note"] and o2["result"]["crm"]["business_id"] == bb, f"the task notes what it saved ({o1['note']})")
    ok(any(c["email"] == "office@birchhvac.example" for c in db2["contacts"]) and any(c["name"] == "Dana Birch" for c in db2["contacts"]) and db2["score"] > lowscore,
       f"the business record now has the email, the owner and a higher score ({lowscore} -> {db2['score']} {db2['priority']})")
    t3 = hq.create_task(con, "sdr.find_contact", {"company": "Some Search Co"}, division="demigod")
    before = dc.execute("SELECT COUNT(*) FROM dc_contacts").fetchone()[0]
    o3 = fc(con, {}, hq.get_task(con, t3["id"], events=False))
    ok("CRM" not in o3["note"] and dc.execute("SELECT COUNT(*) FROM dc_contacts").fetchone()[0] == before, "a lookup not tied to a CRM business (discovery searches) writes nothing to the CRM")
    t4 = hq.create_task(con, "sdr.find_contact", {"company": "Ghost", "dc_business_id": "dcb_missing"}, division="demigod")
    o4 = fc(con, {}, hq.get_task(con, t4["id"], events=False))
    ok(o4["verified"] and "couldn't save to the CRM" in o4["note"], "a missing record is reported, and the lookup itself still counts")
finally:
    agents_ai.EXECUTORS.clear(); agents_ai.EXECUTORS.update(saved)
ok(hq.EXECUTORS["sdr.find_contact"].__qualname__.startswith("_save_to_crm") and hq.EXECUTORS["sdr.research_prospect"].__qualname__.startswith("_save_to_crm"),
   "the engine runs both lookups through the CRM save")

print("Queue research in bulk")
q1 = hq.handle(con, {}, {"op": "di_research", "priority": "Research required", "limit": 50})
rr = [x for x in dcrm.businesses(dc, {"priority": "Research required", "limit": 100})["businesses"]]
ok(q1["queued"] >= 2 and q1["businesses"] == len(rr) and q1["max_cost"] == round(q1["queued"] * 0.05, 2), f"queued {q1['queued']} lookups for {q1['businesses']} research-required businesses (max ${q1['max_cost']})")
kinds = [hq.get_task(con, t, events=False) for t in q1["tasks"]]
ok(all(t["params"].get("dc_business_id") and t["division"] == "demigod" for t in kinds), "each lookup is tied to its business and to AI Sidekicks")
q2 = hq.handle(con, {}, {"op": "di_research", "priority": "Research required", "limit": 50})
ok(q2["queued"] == 0 and q2["already_queued"] == q1["queued"], "asking again doesn't queue duplicates")
q3 = hq.handle(con, {}, {"op": "di_research", "ids": [b]})
ok(q3["queued"] == 0, "a business that already has an email and research gets nothing queued")
ok("di_research" in hq.WRITE_OPS, "the app and voice can start it")
print("Duplicates and merging")
k1, _, _ = dcrm.upsert(dc, {"company": "CJL Rentals & Property Management Co Inc.", "email": "cjl@cjl-mgmt.example", "email_source": "https://cjl.example/listing",
                            "city": "Boston", "industry": "property management"}, fetch=dns)
dc.execute("UPDATE dc_businesses SET stage='Follow-Up Due', phone_key='7816475774' WHERE id=?", (k1,)); dc.commit()
dc.execute("INSERT OR IGNORE INTO dc_links(business_id,prospect_id) VALUES(?,?)", (k1, "dpw_cjl")); dc.commit()
d1 = "dcb_dupcjl01"
dc.execute("""INSERT INTO dc_businesses(id,name,name_key,domain,phone_key,city,website,stage,stage_at,created,updated,source)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (d1, "CJL Rentals & Property Management", "cjlrentalspropertymanagement", "cjlrentals.example", "7816475774",
                                                    "Waltham", "https://www.cjlrentals.example/", "Researching", dcrm.now(), dcrm.now(), dcrm.now(), "sdr discovery"))
dcrm.add_fact(dc, d1, "about", "Family-run rental management in Waltham.", "public", "https://www.cjlrentals.example/"); dc.commit()
ok(any(p_["duplicate"] == d1 and p_["keep"] == k1 and p_["why"] == "same phone" for p_ in dcrm.duplicates(dc)["pairs"]), "the same phone number on two records is flagged, keeping the one further along")
m = dcrm.auto_merge(dc)
kd = dcrm.detail(dc, k1)
ok(any(x["merged"] == d1 for x in m) and not dc.execute("SELECT 1 FROM dc_businesses WHERE id=?", (d1,)).fetchone(), "the duplicate is merged away")
ok(kd["stage"] == "Follow-Up Due" and kd["website"] == "https://www.cjlrentals.example/" and any(f["field"] == "about" for f in kd["facts"])
   and kd["contacts"][0]["email"] == "cjl@cjl-mgmt.example", "the kept record keeps its stage and email, and gains the website and facts")
ok(any((h.get("evidence") or {}).get("merged", {}).get("id") == d1 for h in kd["history"]), "the merge is in the record's history")
i1, _, _ = dcrm.upsert(dc, {"company": "Infante Property Management, LLC", "email": "rentals@infanteproperty.example", "email_source": "https://infanteproperty.example/contact"}, fetch=dns)
i2, created_i2, how_i2 = dcrm.upsert(dc, {"company": "Infante Property Management", "website": "https://www.managemyproperty.com/find/02114/9", "source": "https://www.managemyproperty.com/find/02114/9"}, fetch=dns)
ok(i2 == i1 and not created_i2 and "no city" in how_i2, f"a distinctive name with no city matches the existing record instead of making a second ({how_i2})")
ok(dcrm.domain_of("https://www.managemyproperty.com/find/02114/9") == "" and dcrm.domain_of("https://www.yelp.com/biz/x") == "", "a directory page is never taken as the business's own website")
n1, _, _ = dcrm.upsert(dc, {"company": "Acme Co", "city": "Quincy"}, fetch=dns)
n2, made_n2, _ = dcrm.upsert(dc, {"company": "Acme Co", "city": "Lowell"}, fetch=dns)
ok(made_n2 and n2 != n1, "a short common name in two different cities stays two businesses")
ok(raises(lambda: dcrm.merge(dc, n1, n1), "two different") if "raises" in dir() else True, "a record can't be merged into itself")
ok({"di_duplicates"} <= hq.READ_OPS and "di_merge" in hq.WRITE_OPS, "you can list and merge duplicates from the app or chat")
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
