"""Hunter tests with synthetic fixtures. Run: python -m tests.test_hunter  (uses a throwaway database)."""
import csv, io, json, os, sys, tempfile, hashlib
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, hunter as H, config

tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"
config.ROOT = tmp
(tmp / "config.yaml").write_text("sms: {dnc_file: data/dnc.txt}\n", encoding="utf-8")
(tmp / "data").mkdir()
(tmp / "data" / "dnc.txt").write_text("", encoding="utf-8")
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

con = H.connect()
D = lambda n: (date.today() + timedelta(days=n)).isoformat()
cfg = {"counties": [{"name": "Pinellas", "state": "FL", "adapter": "realauction", "url": "https://pinellas.realforeclose.com", "sale_type": "foreclosure", "enabled": True},
                    {"name": "Pinellas", "state": "FL", "adapter": "realauction", "url": "https://pinellas.realtaxdeed.com", "sale_type": "taxdeed", "enabled": True}],
       "scrape": {"delay_seconds": 0}}
H.sync_registry(con, cfg)
src = dict(con.execute("SELECT * FROM hunter_sources WHERE id='fl-pinellas-realauction-foreclosure'").fetchone())

FIX = [
    {"county": "Pinellas", "state": "FL", "source_type": "foreclosure_auction", "case_number": "22-001234-CI", "parcel_id": "12-34-56-78901-000-0010",
     "address": "1234 MAIN ST N", "city": "ST PETERSBURG", "zip": "33701", "judgment_amount": "$187,342.10", "assessed_value": "$412,000.00", "event_date": D(5), "url": "https://x/1"},
    {"county": "Pinellas", "state": "FL", "source_type": "foreclosure_auction", "case_number": "22-009999-CI", "parcel_id": "99-11",
     "address": "55 OAK AVE", "city": "CLEARWATER", "zip": "33755", "judgment_amount": "$300,000", "assessed_value": "$250,000", "event_date": D(9)},
    {"county": "Pinellas", "state": "FL", "source_type": "foreclosure_auction", "case_number": "", "parcel_id": "", "address": "", "event_date": D(3)},  # invalid
    {"county": "Pinellas", "state": "FL", "source_type": "foreclosure_auction", "case_number": "22-555-CI", "parcel_id": "77-1",
     "address": "TIMESHARE UNIT", "event_date": "not a date", "judgment_amount": "abc"},
]

class FakeAdapter(H.SourceAdapter):
    pages = [FIX[:2], FIX[2:]]
    calls = 0
    def validate_configuration(self): return True, "ok"
    def fetch_records(self, filters=None, cursor=None, log=print, should_stop=lambda: False):
        FakeAdapter.calls += 1
        if FakeAdapter.calls == 1: raise ConnectionError("transient")
        out = []
        for p in self.pages: out += p
        return out

# 1 + 3: adapter retrieves & normalizes; retries don't duplicate
jid = H.request_job(con, src["id"])
import time; H.time = time
orig_sleep = time.sleep; time.sleep = lambda s: None
st = H.run_job(con, cfg, jid, log=lambda *a: None, adapter_cls=FakeAdapter)
time.sleep = orig_sleep
job = con.execute("SELECT * FROM hunter_jobs WHERE id=?", (jid,)).fetchone()
ok(st == "done" and job["accepted"] == 3 and job["rejected"] == 1 and FakeAdapter.calls == 2, "adapter run with retry: 3 accepted, 1 rejected (no address/parcel)")
p = con.execute("SELECT * FROM properties WHERE parcel_id='12-34-56-78901-000-0010'").fetchone()
ok(p and p["street"] == "1234 MAIN ST N" and p["zip"] == "33701" and p["city"] == "St Petersburg" and p["assessed_value"] == 412000, "record normalized")
ev = con.execute("SELECT * FROM lead_events WHERE case_ref='22-001234-CI'").fetchone()
ok(ev and ev["amount"] == 187342.10 and ev["amount_label"] == "final judgment" and ev["source_id"] == src["id"] and ev["raw_id"], "event keeps amount label + provenance")
bad = con.execute("SELECT * FROM lead_events WHERE case_ref='22-555-CI'").fetchone()
ok(bad and bad["event_date"] is None and bad["amount"] is None and "isn't a date" in bad["validation"], "bad date/amount cleared, not invented")
src_row = con.execute("SELECT * FROM hunter_sources WHERE id=?", (src["id"],)).fetchone()
ok(src_row["coverage_status"] == "operational" and src_row["last_success"], "source marked operational after real records")
# rerun = idempotent
jid2 = H.request_job(con, src["id"])
H.run_job(con, cfg, jid2, log=lambda *a: None, adapter_cls=FakeAdapter)
ok(con.execute("SELECT COUNT(*) FROM lead_events").fetchone()[0] == 3 and con.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 3, "re-running a sync creates no duplicates")
# 2: unsupported source is not reported as connected
cov = H.coverage(con)
row = next(m for m in cov["matrix"] if m["county"] == "Pinellas")
ok(row["categories"]["probate"] == "unsupported" and row["categories"]["foreclosure_sale"] == "operational", "unsupported category shown as unsupported")
taxsrc = next(s for s in cov["sources"] if s["category"] == "tax_deed_sale")
ok(taxsrc["coverage_status"] == "configured_untested", "configured-but-never-run source is not shown as operational")
# 4: multi-source match keeps provenance
man = H.ingest_manual(con, "FL", "Pinellas", "code_violation", [{"address": "1234 Main Street North, St Petersburg, FL 33701", "case_number": "CE-77", "status": "open", "amount": "1500", "owner": "SMITH, JOHN", "mailing_address": "9 Elm St, Akron, OH 44301"}], "codes.csv")
pid = p["id"]
evs = con.execute("SELECT * FROM lead_events WHERE property_id=?", (pid,)).fetchall()
ok(man["accepted"] == 1 and len(evs) == 2 and {e["source_id"] for e in evs} == {src["id"], man["source_id"]}, "manual code violation matched to same property; both sources kept")
s = con.execute("SELECT * FROM lead_scores WHERE property_id=?", (pid,)).fetchone()
comps = json.loads(s["components"])
ok(comps["multi"]["points"] > 0 and comps["absentee"]["points"] == 10 and comps["condition"]["points"] > 0, "score uses multiple indicators, absentee, condition")
ok(comps["equity"]["points"] > 0 and "assessed" in comps["equity"]["why"], "equity explained from assessed value vs judgment")
ok(s["tier"] in ("A", "B") and s["next_action"], f"tiered ({s['tier']}, score {s['score']}) with next action")
# same filing in two feeds counts once
H.ingest_manual(con, "FL", "Pinellas", "preforeclosure", [{"address": "1234 MAIN ST N, ST PETERSBURG, FL 33701", "case_number": "22-001234-CI", "status": "open"}], "lp.csv")
comps2 = json.loads(con.execute("SELECT components FROM lead_scores WHERE property_id=?", (pid,)).fetchone()[0])
ok(comps2["multi"]["why"].startswith("2 independent"), "same case number across feeds isn't double-counted")
# negative equity flagged
oak = con.execute("SELECT s.* FROM lead_scores s JOIN properties p ON p.id=s.property_id WHERE p.street='55 OAK AVE'").fetchone()
ok("no equity" in oak["disqualifiers"], "underwater lead flagged")
# sensitive-only -> Review
H.ingest_manual(con, "FL", "Pinellas", "divorce", [{"address": "10 Pine Rd, Tampa, FL 33602", "case_number": "DR-1"}], "d.csv")
dv = con.execute("SELECT s.tier FROM lead_scores s JOIN properties p ON p.id=s.property_id WHERE p.street='10 Pine Rd'").fetchone()
ok(dv["tier"] == "Review", "sensitive-only record goes to Review, not a tier")
# 5: ambiguous owner/contact match -> review queue (tested in import below)
# 6 + 7: batch CSV + manifest
H.set_template(con, H.DEFAULT_TEMPLATE)
pv = H.preview_batch(con, {"tiers": ["A", "B", "C"]})
b = H.create_batch(con, {"tiers": ["A", "B", "C"]})
rows = list(csv.reader(io.StringIO(b["csv"].lstrip("﻿"))))
ok(rows[0] == [c["header"] for c in H.DEFAULT_TEMPLATE] and len(rows) - 1 == b["rows"], "CSV uses the configured headers; row count matches")
mrows = con.execute("SELECT * FROM skip_rows WHERE batch_id=?", (b["batch_id"],)).fetchall()
ok(len(mrows) == b["rows"] and all(r["property_id"] for r in mrows) and Path(b["manifest"]).exists(), "manifest links every row to its lead")
main_row = next(r for r in rows[1:] if r[0].startswith("1234"))
ok(main_row[4] == "John" and main_row[5] == "Smith" and main_row[7].startswith("9 Elm"), "owner name split and mailing address exported")
# formula injection
con.execute("UPDATE properties SET street='=HYPERLINK(1)' WHERE street='55 OAK AVE'")
ok(H._safe_cell("=SUM(A1)") == "'=SUM(A1)" and H._safe_cell("+1") == "'+1", "formula injection neutralized")
con.execute("UPDATE properties SET street='55 OAK AVE' WHERE street='=HYPERLINK(1)'")
ok(con.execute("SELECT status FROM skip_batches WHERE id=?", (b["batch_id"],)).fetchone()[0] == "exported", "batch is 'exported', not 'skip-traced'")
# 8 + 9 + 10: import results
ref_main = next(r["row_ref"] for r in mrows if json.loads(r["exported"])["street"].startswith("1234"))
ret = [
    {"Ref": ref_main, "Address": "1234 MAIN ST N", "Zip": "33701", "First": "John", "Last": "Smith", "Phone1": "(727) 555-1111", "Type1": "Mobile", "Phone2": "727-555-2222", "Type2": "Landline"},
    {"Ref": "", "Address": "55 Oak Avenue", "Zip": "33755", "First": "Mary", "Last": "Jones", "Phone1": "7275553333", "Type1": "Mobile", "Phone2": "", "Type2": ""},
    {"Ref": "", "Address": "999 Nowhere Ln", "Zip": "33000", "First": "X", "Last": "Y", "Phone1": "7275554444", "Type1": "", "Phone2": "", "Type2": ""},
    {"Ref": "", "Address": "1 Bad", "Zip": "", "First": "", "Last": "", "Phone1": "12", "Type1": "", "Phone2": "", "Type2": ""},
    {"Ref": ref_main, "Address": "1234 MAIN ST N", "Zip": "33701", "First": "Robert", "Last": "Brown", "Phone1": "7275556666", "Type1": "", "Phone2": "", "Type2": ""},
]
mapping = {"ref": "Ref", "street": "Address", "zip": "Zip", "first": "First", "last": "Last", "phones": ["Phone1", "Phone2"], "phone_types": ["Type1", "Type2"]}
fh = hashlib.sha1(json.dumps(ret).encode()).hexdigest()
r1 = H.import_results(con, b["batch_id"], ret, mapping, fh)
ok(r1["matched"] == 2 and r1["contacts_added"] == 3, f"2 rows matched, 3 phones stored ({r1})")
ok(r1["unmatched"] == 1 and r1["failed"] == 1 and r1["errors"][0]["reason"] == "no valid phone number", "unmatched and malformed rows reported")
ok(r1["review"] == 1 and any(i["reason"].startswith("Returned name") for i in H.review_items(con)), "name mismatch goes to review queue, not attached")
r2 = H.import_results(con, b["batch_id"], ret, mapping, fh)
ok(r2["contacts_added"] == 0 and r2["duplicates"] == 3 and r2["repeat_file"], "re-importing the same file adds no duplicate contacts")
c = con.execute("SELECT * FROM contact_points WHERE phone='+17275551111'").fetchone()
ok(c["data_available"] == 1 and c["manual_allowed"] == 0 and c["auto_approved"] == 0 and c["consent_reviewed"] == 0, "skip-traced number is data, not consent")
try:
    H.set_contact(con, c["id"], {"auto_approved": True}); ok(False, "auto approval blocked without consent+DNC")
except ValueError:
    ok(True, "auto approval blocked without consent+DNC")
# resolve review: attach to chosen property
rv = next(i for i in H.review_items(con) if i["reason"].startswith("Couldn't match"))
H.resolve_review(con, rv["id"], "attach", pid)
ok(con.execute("SELECT COUNT(*) FROM contact_points WHERE phone='+17275554444' AND property_id=?", (pid,)).fetchone()[0] == 1, "reviewed row attached to the property you picked")
# 14: suppression propagates
n = H.suppress_phone(con, "727-555-1111", "opted out")
c2 = con.execute("SELECT * FROM contact_points WHERE phone='+17275551111'").fetchone()
ok(n == 1 and c2["suppressed"] == 1 and c2["manual_allowed"] == 0, "opt-out suppresses the contact everywhere")
# 11: recalculation with new weights
before = con.execute("SELECT score FROM lead_scores WHERE property_id=?", (pid,)).fetchone()[0]
H.set_weights(con, {"distress": 50})
after = con.execute("SELECT score FROM lead_scores WHERE property_id=?", (pid,)).fetchone()[0]
ok(after != before, f"scores recalculate with new weights ({before} -> {after})")
H.set_weights(con, dict(H.DEFAULT_WEIGHTS))
# 20: summary reconciles with stored rows
sm = H.summary(con)
ok(sm["properties"] == con.execute("SELECT COUNT(*) FROM properties").fetchone()[0] and sm["contacts"] == con.execute("SELECT COUNT(*) FROM contact_points WHERE suppressed=0").fetchone()[0], "summary numbers match stored records")
# pause blocks jobs; cancel works
H.set_source(con, src["id"], paused=1)
try:
    H.request_job(con, src["id"]); ok(False, "paused source refuses jobs")
except ValueError:
    ok(True, "paused source refuses jobs")
H.set_source(con, src["id"], paused=0)
j3 = H.request_job(con, src["id"]); H.cancel_job(con, j3)
ok(con.execute("SELECT status FROM hunter_jobs WHERE id=?", (j3,)).fetchone()[0] == "cancelled", "queued job can be cancelled")
# one broken source doesn't stop others
class Broken(H.SourceAdapter):
    def validate_configuration(self): return True, "ok"
    def fetch_records(self, *a, **k): raise RuntimeError("site changed")
time.sleep = lambda s: None
jb = H.request_job(con, "fl-pinellas-realauction-taxdeed")
stb = H.run_job(con, cfg, jb, log=lambda *a: None, adapter_cls=Broken)
time.sleep = orig_sleep
ok(stb == "failed" and "site changed" in con.execute("SELECT last_error FROM hunter_sources WHERE id='fl-pinellas-realauction-taxdeed'").fetchone()[0], "failed source records its error in isolation")
# dispatcher guards
ok(H.handle(con, cfg, {"op": "summary"})["properties"] >= 3, "dispatcher read op works")
try:
    H.handle(con, cfg, {"op": "drop_tables"}); ok(False, "unknown op rejected")
except ValueError:
    ok(True, "unknown op rejected")
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
