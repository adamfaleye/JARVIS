"""The Hunter: acquisitions intelligence for JARVIS.

Pipeline for every source record:  fetch -> validate -> normalize -> match -> dedupe -> score -> persist -> report.

Storage lives in the engine's SQLite database (same file as the CRM mirror), kept separate from the CRM:
  hunter_sources   source registry (one row per county/municipal source or manual upload channel)
  hunter_jobs      sync jobs with counts, errors, cancel flag
  raw_records      untouched source payloads (evidence), de-duplicated by fingerprint
  properties       canonical properties (parcel + normalized address)
  owners / owner_property   owners kept separate from properties; never merged on similar names
  lead_events      one row per distress event, with provenance back to the source and raw record
  lead_scores      explainable score per property
  hunter_leads     workflow state of each property inside The Hunter (new, review, skip-traced, promoted...)
  skip_batches / skip_rows   AvaData export batches and their manifest
  contact_points   skip-traced phones with separate eligibility flags (data available is not consent)
  review_queue     anything ambiguous waits here for a human

Retrieved records and uploaded files are untrusted data. Nothing in them is treated as an instruction.
"""
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import date, datetime, timedelta

from . import db

# ---------------------------------------------------------------- categories
CATEGORIES = {
    "probate": "Probate / inherited",
    "preforeclosure": "Pre-foreclosure (lis pendens / default)",
    "foreclosure_sale": "Foreclosure sale scheduled",
    "tax_delinquent": "Tax delinquent",
    "tax_lien": "Tax lien",
    "tax_deed_sale": "Tax deed sale scheduled",
    "vacant": "Vacant / abandoned",
    "bankruptcy": "Bankruptcy",
    "code_violation": "Code violation / nuisance",
    "health_safety_lien": "Health / safety lien",
    "eviction": "Eviction filing",
    "divorce": "Divorce filing",
    "absentee": "Absentee owner",
    "high_equity": "High equity",
    "long_held": "Long-held",
    "other": "Other public distress record",
}
SENSITIVE = {"probate", "bankruptcy", "divorce", "eviction"}
# how strong each verified distress indicator is (0-25 component)
DISTRESS_POINTS = {"foreclosure_sale": 25, "preforeclosure": 22, "tax_deed_sale": 22, "tax_lien": 18, "tax_delinquent": 17,
                   "code_violation": 14, "health_safety_lien": 15, "vacant": 15, "probate": 14, "eviction": 8, "bankruptcy": 8,
                   "divorce": 4, "absentee": 0, "high_equity": 0, "long_held": 0, "other": 6}
LEGACY_TYPE = {"foreclosure_auction": "foreclosure_sale", "sheriff_sale": "foreclosure_sale", "tax_deed": "tax_deed_sale",
               "lis_pendens": "preforeclosure", "foreclosure": "preforeclosure", "probate": "probate",
               "code_violation": "code_violation", "eviction": "eviction", "other": "other"}
# source precedence when two sources disagree about a property attribute (higher wins; ties -> newest)
PRECEDENCE = {"official_api": 5, "bulk_download": 5, "appraiser": 5, "portal": 4, "csv_export": 4, "web_extraction": 3, "manual": 2}

DEFAULT_WEIGHTS = {"distress": 25, "equity": 20, "ownership": 10, "condition": 10, "absentee": 10,
                   "multi": 10, "completeness": 5, "contact": 5, "freshness": 5}
DEFAULT_TIERS = {"A": 50, "B": 30}

STATE_ABBR = {"FL", "OH", "WV", "GA", "TX", "NC", "SC", "TN", "AL", "PA", "NY", "NJ", "MI", "IN", "KY", "VA", "MD", "AZ", "CA", "IL", "MA"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hunter_sources(
  id TEXT PRIMARY KEY, state TEXT, county TEXT, municipality TEXT, name TEXT, category TEXT, url TEXT,
  access_method TEXT, record_types TEXT, filters TEXT, fields TEXT, adapter TEXT, config TEXT,
  enabled INTEGER DEFAULT 1, paused INTEGER DEFAULT 0, coverage_status TEXT, last_success TEXT, last_attempt TEXT,
  freshness_days INTEGER, limitations TEXT, rate_limit_s REAL, auth_required INTEGER DEFAULT 0,
  tos_status TEXT, last_error TEXT, retry_status TEXT, schedule TEXT DEFAULT 'daily', created TEXT);
CREATE TABLE IF NOT EXISTS hunter_jobs(
  id INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, requested TEXT, started TEXT, finished TEXT, status TEXT,
  fetched INTEGER DEFAULT 0, accepted INTEGER DEFAULT 0, new_events INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0, errors TEXT, cursor TEXT, cancel INTEGER DEFAULT 0, params TEXT);
CREATE TABLE IF NOT EXISTS raw_records(
  id INTEGER PRIMARY KEY, source_id TEXT, job_id INTEGER, fingerprint TEXT UNIQUE, fetched_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS properties(
  id TEXT PRIMARY KEY, addr_key TEXT, parcel_key TEXT, parcel_id TEXT, street TEXT, unit TEXT, city TEXT, state TEXT,
  zip TEXT, county TEXT, municipality TEXT, property_type TEXT, units INTEGER, lot_size TEXT, sqft REAL, year_built INTEGER,
  assessed_value REAL, assessed_source TEXT, assessed_rank INTEGER, est_value REAL, est_value_source TEXT,
  last_sale_date TEXT, last_sale_price REAL, occupancy TEXT, occupancy_source TEXT, created TEXT, updated TEXT, verified_at TEXT);
CREATE INDEX IF NOT EXISTS prop_addr ON properties(addr_key);
CREATE INDEX IF NOT EXISTS prop_parcel ON properties(parcel_key);
CREATE TABLE IF NOT EXISTS owners(
  id TEXT PRIMARY KEY, name TEXT, name_key TEXT, entity_type TEXT, mailing_address TEXT, mailing_key TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS owner_property(
  owner_id TEXT, property_id TEXT, event_id TEXT, confidence TEXT, created TEXT, PRIMARY KEY(owner_id, property_id));
CREATE TABLE IF NOT EXISTS lead_events(
  id TEXT PRIMARY KEY, event_key TEXT UNIQUE, property_id TEXT, owner_id TEXT, category TEXT, jurisdiction TEXT,
  source_id TEXT, raw_id INTEGER, job_id INTEGER, case_ref TEXT, event_date TEXT, published_date TEXT, retrieved_at TEXT,
  status TEXT, validation TEXT, confidence TEXT, amount REAL, amount_label TEXT, url TEXT, expires TEXT, notes TEXT,
  sensitive INTEGER DEFAULT 0, seen_count INTEGER DEFAULT 1, last_seen TEXT);
CREATE INDEX IF NOT EXISTS ev_prop ON lead_events(property_id);
CREATE TABLE IF NOT EXISTS lead_scores(
  property_id TEXT PRIMARY KEY, score INTEGER, tier TEXT, components TEXT, evidence TEXT, missing TEXT,
  confidence TEXT, calculated_at TEXT, next_action TEXT, disqualifiers TEXT);
CREATE TABLE IF NOT EXISTS hunter_leads(
  property_id TEXT PRIMARY KEY, status TEXT DEFAULT 'new', deal_id TEXT, reason TEXT, skip_status TEXT DEFAULT 'none',
  first_seen TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS skip_batches(
  id TEXT PRIMARY KEY, created TEXT, template TEXT, filters TEXT, selected INTEGER, exported INTEGER, status TEXT,
  submitted_at TEXT, returned_at TEXT, imported INTEGER DEFAULT 0, matched INTEGER DEFAULT 0, unmatched INTEGER DEFAULT 0,
  duplicates INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, review INTEGER DEFAULT 0, file_hashes TEXT DEFAULT '[]', file_path TEXT);
CREATE TABLE IF NOT EXISTS skip_rows(
  batch_id TEXT, row_ref TEXT, property_id TEXT, owner_id TEXT, addr_key TEXT, exported TEXT, PRIMARY KEY(batch_id, row_ref));
CREATE TABLE IF NOT EXISTS contact_points(
  id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, property_id TEXT, owner_id TEXT, name TEXT, phone TEXT, phone_type TEXT,
  email TEXT, provider_status TEXT, batch_id TEXT, returned_at TEXT, confidence TEXT,
  data_available INTEGER DEFAULT 1, identity_verified INTEGER DEFAULT 0, consent_reviewed INTEGER DEFAULT 0,
  dnc_screened INTEGER DEFAULT 0, sms_reviewed INTEGER DEFAULT 0, manual_allowed INTEGER DEFAULT 0,
  auto_approved INTEGER DEFAULT 0, suppressed INTEGER DEFAULT 0, suppressed_reason TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS review_queue(
  id INTEGER PRIMARY KEY, kind TEXT, ref TEXT, payload TEXT, reason TEXT, created TEXT, resolved TEXT, resolution TEXT,
  UNIQUE(kind, ref));
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    return con


def _j(v):
    return json.dumps(v, default=str)


def _uid(p):
    return p + uuid.uuid4().hex[:10]


def _h(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- normalization
def norm_zip(z):
    m = re.search(r"\d{5}", str(z or ""))
    return m.group(0) if m else ""


def norm_parcel(p):
    return re.sub(r"[^A-Z0-9]", "", str(p or "").upper())


def addr_key(street, zip_="", city=""):
    k = db.norm_addr(street)
    if not k or not re.match(r"^\d", k):
        return ""
    return k + "|" + (norm_zip(zip_) or re.sub(r"[^a-z]", "", str(city or "").lower()))


ENTITY_RE = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO|LP|LLP|LTD|TRUST|TRUSTEE|ESTATE|BANK|ASSOCIATION|ASSN|"
                       r"HOLDINGS|PROPERTIES|INVESTMENTS|PARTNERS|CHURCH|CITY OF|COUNTY|AUTHORITY|FUND|MORTGAGE|SERVICING)\b", re.I)


def name_key(n):
    s = re.sub(r"[^A-Z0-9 ]", " ", str(n or "").upper())
    s = re.sub(r"\b(JR|SR|II|III|IV|MR|MRS|MS|DR|ET AL|ETAL|ET UX|ETUX)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def entity_type(n):
    s = str(n or "").upper()
    if not s.strip():
        return "unknown"
    if re.search(r"\bESTATE\b|\bDECEASED\b|\bHEIRS\b", s):
        return "estate"
    if re.search(r"\bTRUST|TRUSTEE\b", s):
        return "trust"
    if ENTITY_RE.search(s):
        return "entity"
    return "person"


def num(v):
    if v in (None, ""):
        return None
    try:
        x = float(re.sub(r"[^0-9.\-]", "", str(v)) or "nan")
        return None if x != x else x
    except ValueError:
        return None


def iso_date(v):
    """'' if empty, ISO date if parseable, None if present but not a date."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s.split(" ")[0] if "/" in s or "-" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def split_street(addr):
    """'123 MAIN ST N | ST PETERSBURG, FL- 33701' or '123 Main St, Tampa, FL 33602' -> street, city, state, zip."""
    s = str(addr or "").replace("|", ",").strip()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    street, city, state, zip_ = (parts[0] if parts else ""), "", "", ""
    rest = " ".join(parts[1:])
    ms = [m for m in re.finditer(r"\b([A-Z]{2})\b[-\s]*(\d{5})?", rest.upper()) if m.group(1) in STATE_ABBR]
    m = ms[-1] if ms else None
    if m:
        state, zip_ = m.group(1), m.group(2) or ""
        city = rest[:m.start()].strip(" ,-") if m.start() else (parts[1] if len(parts) > 2 else "")
    elif len(parts) > 1:
        city = parts[1]
    return street, city, state, zip_


# ---------------------------------------------------------------- source registry
def source_id_for(c):
    st = c.get("sale_type") or ""
    return re.sub(r"[^a-z0-9]+", "-", f"{c.get('state','')}-{c.get('name','')}-{c.get('adapter','')}-{st}".lower()).strip("-")


def _cat_for_county(c):
    st = (c.get("sale_type") or "").lower()
    return {"foreclosure": "foreclosure_sale", "taxdeed": "tax_deed_sale", "sheriff": "foreclosure_sale"}.get(st, "other")


ACCESS = {"realauction": "portal", "ai_page": "web_extraction", "csv_url": "csv_export"}


def sync_registry(con, cfg):
    """Make sure every county in counties.yaml is in the registry. Never deletes; disabled ones stay listed."""
    seen = []
    for c in cfg.get("counties", []):
        sid = source_id_for(c)
        seen.append(sid)
        cat = _cat_for_county(c) if c.get("adapter") == "realauction" else (c.get("category") or "other")
        exists = con.execute("SELECT id FROM hunter_sources WHERE id=?", (sid,)).fetchone()
        name = {"foreclosure_sale": f"{c['name']} County foreclosure auction calendar",
                "tax_deed_sale": f"{c['name']} County tax deed auction calendar"}.get(cat, f"{c['name']} {c.get('adapter')}")
        if not exists:
            limitations = ("Auction preview lists upcoming sales only (case, parcel, address, judgment or opening bid, assessed value). "
                           "No owner names or mailing addresses." if c.get("adapter") == "realauction" else "")
            con.execute("""INSERT INTO hunter_sources(id,state,county,name,category,url,access_method,record_types,fields,adapter,config,
                           enabled,coverage_status,freshness_days,limitations,rate_limit_s,tos_status,created)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, c.get("state", ""), c.get("name", ""), name, cat, c.get("url", ""), ACCESS.get(c.get("adapter"), "web_extraction"),
                         _j([cat]), _j(["case_number", "parcel_id", "address", "city", "zip", "judgment_amount", "assessed_value", "event_date"]),
                         c.get("adapter"), _j(c), 1 if c.get("enabled", True) else 0, "configured_untested", 1, limitations,
                         float((cfg.get("scrape") or {}).get("delay_seconds", 3)), "public auction calendar; no login; paced requests; stops on CAPTCHA",
                         now()))
        else:
            con.execute("UPDATE hunter_sources SET url=?, config=?, enabled=? WHERE id=?", (c.get("url", ""), _j(c), 1 if c.get("enabled", True) else 0, sid))
    con.commit()
    return seen


def ensure_manual_source(con, state, county, category, label=""):
    sid = re.sub(r"[^a-z0-9]+", "-", f"manual-{state}-{county}-{category}".lower()).strip("-")
    if not con.execute("SELECT id FROM hunter_sources WHERE id=?", (sid,)).fetchone():
        con.execute("""INSERT INTO hunter_sources(id,state,county,name,category,access_method,record_types,adapter,enabled,coverage_status,
                       limitations,tos_status,created,schedule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, state, county, label or f"{county} {CATEGORIES.get(category, category)} (manual upload)", category, "manual",
                     _j([category]), "manual", 1, "manual", "No automated source connected. Records come from files you upload.",
                     "you are responsible for how the uploaded file was obtained", now(), "manual"))
        con.commit()
    return sid


def coverage(con, cfg=None):
    """What's actually supported: per source, plus a county x category matrix with honest statuses."""
    rows = [dict(r) for r in con.execute("SELECT * FROM hunter_sources ORDER BY state, county, category")]
    for r in rows:
        for k in ("record_types", "fields", "config"):
            try:
                r[k] = json.loads(r[k] or "null")
            except Exception:
                pass
        r["events"] = con.execute("SELECT COUNT(*) FROM lead_events WHERE source_id=?", (r["id"],)).fetchone()[0]
    counties = sorted({(r["state"], r["county"]) for r in rows if r["county"]})
    matrix = []
    for st, co in counties:
        cell = {}
        for cat in CATEGORIES:
            srcs = [r for r in rows if r["state"] == st and r["county"] == co and r["category"] == cat]
            if any(s["coverage_status"] == "operational" and not s["paused"] for s in srcs):
                cell[cat] = "operational"
            elif any(s["access_method"] == "manual" for s in srcs):
                cell[cat] = "manual"
            elif srcs:
                cell[cat] = srcs[0]["coverage_status"]
            else:
                cell[cat] = "unsupported"
        matrix.append({"state": st, "county": co, "categories": cell})
    return {"sources": rows, "matrix": matrix, "categories": CATEGORIES}


def set_source(con, sid, **kw):
    allowed = {"paused", "enabled", "schedule", "tos_status", "limitations", "rate_limit_s"}
    sets = {k: v for k, v in kw.items() if k in allowed}
    if not sets:
        return False
    con.execute(f"UPDATE hunter_sources SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), sid))
    con.commit()
    return True


# ---------------------------------------------------------------- adapter contract
class SourceAdapter:
    """Common contract. Each source gets its own instance, config, logging and failure isolation."""

    def __init__(self, src, cfg):
        self.src, self.cfg = src, cfg
        self.conf = json.loads(src.get("config") or "{}") if isinstance(src.get("config"), str) else (src.get("config") or {})
        self.errors, self._cursor = [], None

    def validate_configuration(self):
        url = self.conf.get("url") or self.src.get("url") or ""
        if self.src.get("access_method") == "manual":
            return True, "Manual uploads need no connection."
        if not url.startswith("https://"):
            return False, "Source URL must be an https:// address."
        return True, "ok"

    def test_connection(self):
        ok, why = self.validate_configuration()
        if not ok or self.src.get("access_method") == "manual":
            return ok, why
        import urllib.request
        try:
            req = urllib.request.Request(self.conf.get("url") or self.src["url"], headers={"User-Agent": "Mozilla/5.0 JARVIS-Hunter"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200, f"HTTP {r.status}"
        except Exception as e:
            return False, f"{e.__class__.__name__}: {str(e)[:120]}"

    def discover_available_record_types(self):
        return [self.src.get("category")]

    def fetch_records(self, filters=None, cursor=None, log=print, should_stop=lambda: False):
        if self.src.get("access_method") == "manual":
            return []
        from .adapters import get as get_adapter
        cfg = dict(self.cfg)
        cfg["_should_stop"] = should_stop
        if filters and filters.get("days_ahead"):
            cfg["scrape"] = dict(cfg.get("scrape") or {}, days_ahead=int(filters["days_ahead"]))
        return get_adapter(self.conf.get("adapter") or self.src.get("adapter")).fetch(self.conf, cfg, log)

    def normalize_record(self, raw):
        return normalize(raw, self.src)

    def validate_record(self, rec):
        return validate(rec)

    def get_next_cursor(self):
        return self._cursor

    def report_coverage(self):
        return {"source": self.src["id"], "categories": self.discover_available_record_types(), "status": self.src.get("coverage_status")}

    def report_errors(self):
        return self.errors


def normalize(raw, src):
    """Map an adapter/manual record onto canonical fields. Unknown stays empty; nothing is guessed."""
    r = {k: (v.strip() if isinstance(v, str) else v) for k, v in (raw or {}).items() if k != "raw"}
    cat = r.get("category") or LEGACY_TYPE.get(str(r.get("source_type") or "").lower()) or src.get("category") or "other"
    street, city, state, zip_ = r.get("address") or "", r.get("city") or "", r.get("state") or src.get("state") or "", r.get("zip") or ""
    if street and ("," in street or "|" in street) and not city:
        s2, c2, st2, z2 = split_street(street)
        street, city, state, zip_ = s2, city or c2, state or st2, zip_ or z2
    unit = ""
    mu = re.search(r"\s(?:APT|UNIT|STE|#)\s*([A-Z0-9-]+)$", street.upper())
    if mu:
        unit, street = mu.group(1), street[:mu.start()].strip()
    status = (r.get("status") or "").lower() or ("scheduled" if cat in ("foreclosure_sale", "tax_deed_sale") else "open")
    return {
        "category": cat if cat in CATEGORIES else "other",
        "street": street, "unit": unit, "city": city.title() if city.isupper() else city, "state": (state or "").upper()[:2],
        "zip": norm_zip(zip_), "county": r.get("county") or src.get("county") or "", "municipality": r.get("municipality") or "",
        "parcel_id": str(r.get("parcel_id") or "").strip(), "owner": r.get("owner") or "",
        "mailing_address": r.get("mailing_address") or "",
        "case_ref": str(r.get("case_number") or r.get("case_ref") or "").strip(),
        "event_date": r.get("event_date") or r.get("filing_date") or "",
        "published_date": r.get("published_date") or "",
        "amount": r.get("judgment_amount") if r.get("judgment_amount") not in (None, "") else r.get("amount"),
        "amount_label": r.get("amount_label") or ({"tax_deed_sale": "opening bid", "foreclosure_sale": "final judgment",
                                                  "tax_delinquent": "delinquent balance", "code_violation": "fine/lien balance"}.get(cat, "amount")),
        "assessed_value": r.get("assessed_value"), "property_type": r.get("property_type") or "",
        "last_sale_date": r.get("last_sale_date") or "", "last_sale_price": r.get("last_sale_price"),
        "year_built": r.get("year_built"), "sqft": r.get("sqft"),
        "status": status, "url": r.get("url") or "", "notes": r.get("notes") or "",
    }


def validate(rec):
    """Returns (ok, reasons, rec). Bad values become unknown or reject the record; never invented."""
    reasons = []
    if not rec["street"] and not rec["parcel_id"]:
        return False, ["no address or parcel ID"], rec
    if rec["street"] and not re.match(r"^\d", rec["street"].strip()):
        reasons.append(f"street '{rec['street'][:40]}' has no house number (kept for review)")
    for f in ("event_date", "published_date", "last_sale_date"):
        if rec[f]:
            d = iso_date(rec[f])
            if d is None:
                reasons.append(f"{f} '{rec[f]}' isn't a date (cleared)")
                rec[f] = ""
            else:
                rec[f] = d
    for f in ("amount", "assessed_value", "last_sale_price", "sqft"):
        v = num(rec[f]) if rec[f] not in (None, "") else None
        if rec[f] not in (None, "") and (v is None or v < 0):
            reasons.append(f"{f} '{rec[f]}' isn't a valid number (cleared)")
        rec[f] = v if v is not None and v >= 0 else None
    yb = num(rec["year_built"])
    rec["year_built"] = int(yb) if yb and 1700 < yb <= date.today().year + 1 else None
    if rec["state"] and len(rec["state"]) != 2:
        rec["state"] = ""
    return True, reasons, rec


# ---------------------------------------------------------------- match + persist
def _precedence(src):
    return PRECEDENCE.get(src.get("access_method") or "", 3)


def match_property(con, rec):
    """Parcel first (same county), then normalized address. Returns (property_id or None, how)."""
    pk = norm_parcel(rec["parcel_id"])
    if pk:
        r = con.execute("SELECT id FROM properties WHERE parcel_key=? AND lower(county)=lower(?)", (pk, rec["county"])).fetchone()
        if r:
            return r["id"], "parcel"
    ak = addr_key(rec["street"], rec["zip"], rec["city"])
    if ak:
        rows = con.execute("SELECT id, parcel_key FROM properties WHERE addr_key=?", (ak,)).fetchall()
        rows = [x for x in rows if not (pk and x["parcel_key"] and x["parcel_key"] != pk)]  # same address, different parcel = different property
        if len(rows) == 1:
            return rows[0]["id"], "address"
        if len(rows) > 1:
            return None, "ambiguous"
    return None, "new"


def upsert_property(con, rec, src):
    pid, how = match_property(con, rec)
    ts, prec = now(), _precedence(src)
    ak, pk = addr_key(rec["street"], rec["zip"], rec["city"]), norm_parcel(rec["parcel_id"])
    if how == "ambiguous":
        pid = None
    if not pid:
        pid = _uid("p_")
        con.execute("""INSERT INTO properties(id,addr_key,parcel_key,parcel_id,street,unit,city,state,zip,county,municipality,property_type,
                       sqft,year_built,assessed_value,assessed_source,assessed_rank,last_sale_date,last_sale_price,created,updated,verified_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, ak, pk, rec["parcel_id"], rec["street"], rec["unit"], rec["city"], rec["state"], rec["zip"], rec["county"],
                     rec["municipality"], rec["property_type"], rec["sqft"], rec["year_built"], rec["assessed_value"],
                     src["id"] if rec["assessed_value"] is not None else None, prec if rec["assessed_value"] is not None else None,
                     rec["last_sale_date"] or None, rec["last_sale_price"], ts, ts, ts))
        if how == "ambiguous":
            add_review(con, "property_match", pid, {"address": rec["street"], "zip": rec["zip"]},
                       "Same address as more than one existing property; kept separate until you decide.")
        return pid, "new" if how != "ambiguous" else "ambiguous"
    p = dict(con.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone())
    upd = {"updated": ts, "verified_at": ts}
    # fill blanks; never overwrite a known value with an unknown one
    for col, val in (("parcel_id", rec["parcel_id"]), ("parcel_key", pk), ("street", rec["street"]), ("unit", rec["unit"]),
                     ("city", rec["city"]), ("state", rec["state"]), ("zip", rec["zip"]), ("municipality", rec["municipality"]),
                     ("property_type", rec["property_type"]), ("sqft", rec["sqft"]), ("year_built", rec["year_built"]),
                     ("addr_key", ak)):
        if val not in (None, "") and p.get(col) in (None, ""):
            upd[col] = val
    if rec["assessed_value"] is not None and (p.get("assessed_rank") is None or prec >= (p.get("assessed_rank") or 0)):
        upd.update(assessed_value=rec["assessed_value"], assessed_source=src["id"], assessed_rank=prec)
    if rec["last_sale_date"] and (not p.get("last_sale_date") or rec["last_sale_date"] > p["last_sale_date"]):
        upd.update(last_sale_date=rec["last_sale_date"], last_sale_price=rec["last_sale_price"])
    con.execute(f"UPDATE properties SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), pid))
    return pid, how


def upsert_owner(con, rec, pid, event_id):
    n = (rec.get("owner") or "").strip()
    if not n:
        return None
    nk, mk = name_key(n), db.norm_addr(rec.get("mailing_address"))
    # identity = exact normalized name + mailing address; without a mailing address the owner is scoped to this property
    oid = "o_" + _h(nk, mk or ("prop:" + pid))
    if not con.execute("SELECT id FROM owners WHERE id=?", (oid,)).fetchone():
        con.execute("INSERT INTO owners(id,name,name_key,entity_type,mailing_address,mailing_key,created) VALUES(?,?,?,?,?,?,?)",
                    (oid, n, nk, entity_type(n), rec.get("mailing_address") or "", mk, now()))
    con.execute("INSERT OR IGNORE INTO owner_property(owner_id,property_id,event_id,confidence,created) VALUES(?,?,?,?,?)",
                (oid, pid, event_id, "source-listed", now()))
    return oid


def event_status(rec):
    st = rec["status"]
    if rec["category"] in ("foreclosure_sale", "tax_deed_sale") and rec["event_date"]:
        try:
            if date.fromisoformat(rec["event_date"]) < date.today():
                return "sale date passed: verify outcome"
        except ValueError:
            pass
    return st or "open"


def ingest(con, src, records, job_id=None, log=print):
    """Run records through the pipeline. Idempotent: re-running the same records adds nothing new."""
    stats = {"fetched": len(records), "accepted": 0, "new_events": 0, "duplicates": 0, "rejected": 0, "errors": []}
    touched = set()
    for raw in records:
        try:
            payload = _j(raw)
            fp = _h(src["id"], payload)
            cur = con.execute("INSERT OR IGNORE INTO raw_records(source_id,job_id,fingerprint,fetched_at,payload) VALUES(?,?,?,?,?)",
                              (src["id"], job_id, fp, now(), payload))
            raw_id = cur.lastrowid if cur.rowcount else con.execute("SELECT id FROM raw_records WHERE fingerprint=?", (fp,)).fetchone()["id"]
            rec = normalize(raw, src)
            ok, reasons, rec = validate(rec)
            if not ok:
                stats["rejected"] += 1
                stats["errors"].append({"record": str(raw.get("case_number") or raw.get("address") or "")[:60], "reason": "; ".join(reasons)})
                continue
            pid, how = upsert_property(con, rec, src)
            jur = f"{rec['county']} {rec['state']}".strip()
            ek = _h(rec["category"], jur.lower(), rec["case_ref"].upper()) if rec["case_ref"] else \
                _h(src["id"], rec["category"], addr_key(rec["street"], rec["zip"], rec["city"]) or norm_parcel(rec["parcel_id"]), rec["event_date"])
            ex = con.execute("SELECT id FROM lead_events WHERE event_key=?", (ek,)).fetchone()
            if ex:
                con.execute("UPDATE lead_events SET seen_count=seen_count+1, last_seen=?, status=? WHERE id=?", (now(), event_status(rec), ex["id"]))
                stats["duplicates"] += 1
                eid = ex["id"]
            else:
                eid = _uid("e_")
                exp = (date.today() + timedelta(days=30 if rec["category"] in ("foreclosure_sale", "tax_deed_sale") else 90)).isoformat()
                con.execute("""INSERT INTO lead_events(id,event_key,property_id,category,jurisdiction,source_id,raw_id,job_id,case_ref,
                               event_date,published_date,retrieved_at,status,validation,confidence,amount,amount_label,url,expires,notes,
                               sensitive,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (eid, ek, pid, rec["category"], jur, src["id"], raw_id, job_id, rec["case_ref"], rec["event_date"] or None,
                             rec["published_date"] or None, now(), event_status(rec), "; ".join(reasons) or "ok",
                             "high" if rec["case_ref"] and (rec["parcel_id"] or rec["street"]) else "medium",
                             rec["amount"], rec["amount_label"], rec["url"], exp, rec["notes"][:500], 1 if rec["category"] in SENSITIVE else 0, now()))
                stats["new_events"] += 1
            oid = upsert_owner(con, rec, pid, eid)
            if oid:
                con.execute("UPDATE lead_events SET owner_id=COALESCE(owner_id, ?) WHERE id=?", (oid, eid))
            con.execute("INSERT OR IGNORE INTO hunter_leads(property_id,status,first_seen,updated) VALUES(?,?,?,?)", (pid, "new", now(), now()))
            touched.add(pid)
            stats["accepted"] += 1
        except Exception as e:  # one bad record never stops the batch
            stats["rejected"] += 1
            stats["errors"].append({"record": str(raw)[:60], "reason": f"{e.__class__.__name__}: {e}"})
    con.commit()
    w = weights(con)
    for pid in touched:
        score_property(con, pid, w)
    con.commit()
    return stats


# ---------------------------------------------------------------- jobs
def request_job(con, source_id, kind="sync", params=None):
    src = con.execute("SELECT * FROM hunter_sources WHERE id=?", (source_id,)).fetchone()
    if not src:
        raise ValueError("Unknown source.")
    if src["access_method"] == "manual":
        raise ValueError("This is a manual-upload source. Upload a file instead.")
    if src["paused"]:
        raise ValueError("This source is paused. Resume it first.")
    busy = con.execute("SELECT id FROM hunter_jobs WHERE source_id=? AND status IN ('queued','running')", (source_id,)).fetchone()
    if busy:
        return busy["id"]
    cur = con.execute("INSERT INTO hunter_jobs(source_id,kind,requested,status,params) VALUES(?,?,?,?,?)",
                      (source_id, kind, now(), "queued", _j(params or {})))
    con.commit()
    return cur.lastrowid


def cancel_job(con, job_id):
    con.execute("UPDATE hunter_jobs SET cancel=1 WHERE id=? AND status IN ('queued','running')", (job_id,))
    con.execute("UPDATE hunter_jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'", (now(), job_id))
    con.commit()


def run_job(con, cfg, job_id, log=print, adapter_cls=None):
    job = con.execute("SELECT * FROM hunter_jobs WHERE id=?", (job_id,)).fetchone()
    src = dict(con.execute("SELECT * FROM hunter_sources WHERE id=?", (job["source_id"],)).fetchone())
    if job["cancel"]:
        return
    con.execute("UPDATE hunter_jobs SET status='running', started=? WHERE id=?", (now(), job_id))
    con.execute("UPDATE hunter_sources SET last_attempt=? WHERE id=?", (now(), src["id"]))
    con.commit()
    ad = (adapter_cls or SourceAdapter)(src, cfg)

    def should_stop():
        r = con.execute("SELECT cancel FROM hunter_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(r and r["cancel"])

    status, err, stats = "done", None, {"fetched": 0, "accepted": 0, "new_events": 0, "duplicates": 0, "rejected": 0, "errors": []}
    try:
        ok, why = ad.validate_configuration()
        if not ok:
            raise ValueError(why)
        params = json.loads(job["params"] or "{}")
        recs = []
        for attempt in range(3):  # retry with backoff on transient failures
            try:
                recs = ad.fetch_records(params, None, log, should_stop)
                break
            except Exception as e:
                if e.__class__.__name__ == "Blocked" or attempt == 2:
                    raise
                import time
                time.sleep(2 ** (attempt + 1))
        stats = ingest(con, src, recs, job_id, log)
        if should_stop():
            status = "cancelled"
    except Exception as e:
        status, err = "failed", f"{e.__class__.__name__}: {str(e)[:300]}"
    con.execute("""UPDATE hunter_jobs SET status=?, finished=?, fetched=?, accepted=?, new_events=?, duplicates=?, rejected=?, errors=?
                   WHERE id=?""", (status, now(), stats["fetched"], stats["accepted"], stats["new_events"], stats["duplicates"],
                                   stats["rejected"], _j(stats["errors"][:50] + ([{"reason": err}] if err else [])), job_id))
    if status == "done":
        cov = "operational" if stats["accepted"] > 0 else src["coverage_status"] if src["coverage_status"] == "operational" else "connected_no_records"
        con.execute("UPDATE hunter_sources SET last_success=?, coverage_status=?, last_error=NULL, retry_status=NULL WHERE id=?", (now(), cov, src["id"]))
    elif status == "failed":
        con.execute("UPDATE hunter_sources SET last_error=?, retry_status=?, coverage_status=CASE WHEN coverage_status='operational' THEN 'operational' ELSE 'error' END WHERE id=?",
                    (err, "will retry on next scheduled run", src["id"]))
    con.commit()
    return status


def run_queued(con, cfg, log=print):
    """Called from the engine loop: run any jobs the app requested (one at a time, oldest first)."""
    done = 0
    for j in con.execute("SELECT id FROM hunter_jobs WHERE status='queued' AND cancel=0 ORDER BY id").fetchall():
        log(f"Hunter job {j['id']} starting")
        run_job(con, cfg, j["id"], log)
        done += 1
    return done


def record_scrape(con, county_cfg, recs, log=print):
    """Hook for the daily scrape: ingest what the existing adapter already fetched, as a job with provenance."""
    sid = source_id_for(county_cfg)
    src = con.execute("SELECT * FROM hunter_sources WHERE id=?", (sid,)).fetchone()
    if not src:
        return None
    if src["paused"]:
        log(f"  Hunter: {sid} is paused; records not ingested.")
        return None
    src = dict(src)
    cur = con.execute("INSERT INTO hunter_jobs(source_id,kind,requested,started,status) VALUES(?,?,?,?,?)", (sid, "scheduled", now(), now(), "running"))
    jid = cur.lastrowid
    stats = ingest(con, src, recs, jid, log)
    con.execute("UPDATE hunter_jobs SET status='done', finished=?, fetched=?, accepted=?, new_events=?, duplicates=?, rejected=?, errors=? WHERE id=?",
                (now(), stats["fetched"], stats["accepted"], stats["new_events"], stats["duplicates"], stats["rejected"], _j(stats["errors"][:50]), jid))
    cov = "operational" if stats["accepted"] else (src["coverage_status"] or "connected_no_records")
    con.execute("UPDATE hunter_sources SET last_attempt=?, last_success=?, coverage_status=? WHERE id=?", (now(), now(), cov, sid))
    con.commit()
    return stats


def is_paused(con, county_cfg):
    r = con.execute("SELECT paused FROM hunter_sources WHERE id=?", (source_id_for(county_cfg),)).fetchone()
    return bool(r and r["paused"])


def backfill_legacy(con, cfg, log=print):
    """One-time: bring rows from the original county-lead table into The Hunter (with their source)."""
    if db.kv_get(con, "hunter_backfilled") == "1":
        return 0
    by_src = {}
    for r in con.execute("SELECT * FROM leads"):
        c = next((c for c in cfg.get("counties", []) if c.get("name", "").lower() == (r["county"] or "").lower()
                  and LEGACY_TYPE.get(r["source_type"] or "") == _cat_for_county(c)), None)
        if not c:
            continue
        by_src.setdefault(source_id_for(c), []).append({k: r[k] for k in r.keys() if k not in ("id", "key", "raw", "first_seen", "status")})
    n = 0
    for sid, recs in by_src.items():
        src = con.execute("SELECT * FROM hunter_sources WHERE id=?", (sid,)).fetchone()
        if src:
            n += ingest(con, dict(src), recs, None, log)["accepted"]
            con.execute("UPDATE hunter_sources SET coverage_status='operational', last_success=COALESCE(last_success, ?) WHERE id=?", (now(), sid))
    db.kv_set(con, "hunter_backfilled", "1")
    con.commit()
    return n


# ---------------------------------------------------------------- scoring
def weights(con):
    try:
        w = json.loads(db.kv_get(con, "hunter_weights") or "{}")
    except Exception:
        w = {}
    out = dict(DEFAULT_WEIGHTS)
    out.update({k: max(0, int(v)) for k, v in w.items() if k in DEFAULT_WEIGHTS and str(v).lstrip("-").isdigit()})
    try:
        t = json.loads(db.kv_get(con, "hunter_tiers") or "{}")
    except Exception:
        t = {}
    out["_tiers"] = {"A": int(t.get("A", DEFAULT_TIERS["A"])), "B": int(t.get("B", DEFAULT_TIERS["B"]))}
    return out


def set_weights(con, w=None, tiers=None):
    if w is not None:
        db.kv_set(con, "hunter_weights", _j({k: int(v) for k, v in w.items() if k in DEFAULT_WEIGHTS}))
    if tiers is not None:
        db.kv_set(con, "hunter_tiers", _j({k: int(v) for k, v in tiers.items() if k in ("A", "B")}))
    w2 = weights(con)
    n = 0
    for r in con.execute("SELECT id FROM properties").fetchall():
        score_property(con, r["id"], w2)
        n += 1
    con.commit()
    return n


def score_property(con, pid, w=None):
    """Explainable score. Each component: points, max, why. Weights are starting defaults, not proven predictors."""
    w = w or weights(con)
    p = dict(con.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone())
    evs = [dict(r) for r in con.execute("SELECT * FROM lead_events WHERE property_id=?", (pid,))]
    contacts = [dict(r) for r in con.execute("SELECT * FROM contact_points WHERE property_id=? AND suppressed=0", (pid,))]
    owners = [dict(r) for r in con.execute("SELECT o.* FROM owners o JOIN owner_property op ON op.owner_id=o.id WHERE op.property_id=?", (pid,))]
    today = date.today()
    comps, evidence, missing, disq = {}, [], [], []

    def comp(key, frac, why):
        pts = round(w[key] * max(0.0, min(1.0, frac)))
        comps[key] = {"points": pts, "max": w[key], "why": why}

    live = [e for e in evs if not str(e["status"]).startswith("sale date passed") and e["status"] not in ("closed", "resolved", "dismissed")]
    # 1. distress: strongest live indicator (sensitive categories count as context, not proof)
    best = max(live, key=lambda e: DISTRESS_POINTS.get(e["category"], 0), default=None)
    if best:
        comp("distress", DISTRESS_POINTS.get(best["category"], 0) / 25, f"{CATEGORIES.get(best['category'])} ({best['jurisdiction']}"
             + (f", case {best['case_ref']}" if best["case_ref"] else "") + (f", {best['event_date']}" if best["event_date"] else "") + ")")
    else:
        comp("distress", 0, "No current distress record (sale date passed or resolved)" if evs else "No distress record")
        if evs:
            disq.append("Its only distress record is past or resolved; verify before working it.")
    for e in evs:
        evidence.append({"category": e["category"], "label": CATEGORIES.get(e["category"]), "source": e["source_id"], "case": e["case_ref"],
                         "date": e["event_date"], "status": e["status"], "amount": e["amount"], "amount_label": e["amount_label"], "url": e["url"]})
    # 2. equity: only from published numbers; assessed value is a county number, not market value
    val = p.get("est_value") or p.get("assessed_value")
    debt = max([e["amount"] for e in live if e["amount"] and e["category"] in ("foreclosure_sale", "preforeclosure", "tax_lien", "tax_delinquent", "tax_deed_sale")] or [0])
    if val and debt:
        eq = (val - debt) / val
        frac = 1 if eq >= .5 else .7 if eq >= .3 else .4 if eq >= .15 else .15 if eq > 0 else 0
        comp("equity", frac, f"~{eq:.0%} est. equity: {'estimated value' if p.get('est_value') else 'county assessed value'} ${val:,.0f} vs "
             f"{'amount at sale' if best and best['category'] in ('foreclosure_sale','tax_deed_sale') else 'recorded amount'} ${debt:,.0f}. "
             "Other liens/mortgages unknown.")
        if eq <= 0:
            disq.append("Published amount owed exceeds the county value: likely no equity.")
    elif val:
        comp("equity", 0, "Value known but no published debt amount; equity unknown")
        missing.append("amount owed / mortgage balance")
    else:
        comp("equity", 0, "Equity unknown")
        missing.append("property value")
    # 3. ownership duration
    if p.get("last_sale_date"):
        yrs = (today - date.fromisoformat(p["last_sale_date"][:10])).days / 365.25
        comp("ownership", 1 if yrs >= 15 else .7 if yrs >= 10 else .4 if yrs >= 5 else 0, f"Owned ~{yrs:.0f} years (last recorded sale {p['last_sale_date'][:10]})")
    else:
        comp("ownership", 0, "Ownership length unknown")
        missing.append("last sale date")
    # 4. condition indicators (code/health records only; never inferred)
    cond = [e for e in live if e["category"] in ("code_violation", "health_safety_lien", "vacant")]
    comp("condition", 1 if len(cond) > 1 else .7 if cond else 0, f"{len(cond)} open condition record(s)" if cond else "No condition records")
    # 5. absentee evidence: mailing address on file differs from the property
    mails = [o for o in owners if o.get("mailing_key")]
    prop_k = db.norm_addr(p.get("street"))
    if mails:
        absent = any(o["mailing_key"] != prop_k for o in mails)
        comp("absentee", 1 if absent else 0, "Owner mailing address differs from the property" if absent else "Owner mails to the property")
    else:
        comp("absentee", 0, "Owner mailing address unknown")
        missing.append("owner mailing address")
    # 6. multiple independent indicators (same case number across feeds counts once)
    indep = {}
    for e in live:
        indep.setdefault(e["case_ref"].upper() if e["case_ref"] else f"{e['category']}|{e['source_id']}", e["category"])
    cats = set(indep.values())
    comp("multi", 1 if len(cats) >= 3 else .6 if len(cats) == 2 else 0, f"{len(cats)} independent distress categories")
    # 7. completeness
    fields = ["street", "city", "zip", "parcel_id", "assessed_value", "last_sale_date", "sqft", "year_built"]
    have = sum(1 for f in fields if p.get(f) not in (None, ""))
    if not owners:
        missing.append("owner name")
    comp("completeness", (have + (1 if owners else 0)) / (len(fields) + 1), f"{have + (1 if owners else 0)} of {len(fields) + 1} key fields known")
    # 8. contact match confidence
    hi = [c for c in contacts if c["confidence"] == "high"]
    comp("contact", 1 if hi else .5 if contacts else 0, f"{len(contacts)} skip-traced number(s), {len(hi)} high-confidence match" if contacts else "Not skip-traced yet")
    # 9. freshness
    newest = max([e["retrieved_at"] for e in evs] or [""])
    if newest:
        age = (datetime.now() - datetime.fromisoformat(newest)).days
        comp("freshness", 1 if age <= 30 else .6 if age <= 90 else .2 if age <= 180 else 0, f"Last confirmed {age} day(s) ago")
    else:
        comp("freshness", 0, "Never confirmed")
    score = sum(c["points"] for c in comps.values())
    maxpts = sum(w[k] for k in DEFAULT_WEIGHTS)
    score = round(score * 100 / maxpts) if maxpts else 0
    review_reasons = []
    if not p.get("street") or not re.match(r"^\d", p.get("street") or ""):
        review_reasons.append("No usable street address")
    if live and all(e["category"] in SENSITIVE for e in live):
        review_reasons.append("Only a sensitive record (probate/bankruptcy/eviction/divorce); verify before any outreach")
    if any(e["category"] == "bankruptcy" for e in live):
        review_reasons.append("Bankruptcy: legal restrictions may apply; get review before contact or contract")
    if not live:
        review_reasons.append("No current distress record")
    tier = "Review" if review_reasons else ("A" if score >= w["_tiers"]["A"] else "B" if score >= w["_tiers"]["B"] else "C")
    conf = "high" if len(missing) <= 2 else "medium" if len(missing) <= 4 else "low"
    hl = con.execute("SELECT * FROM hunter_leads WHERE property_id=?", (pid,)).fetchone()
    skip = hl["skip_status"] if hl else "none"
    if tier == "Review":
        nxt = "Review: " + review_reasons[0]
    elif skip in ("none",) and not contacts:
        nxt = "Add to a skip-trace batch"
    elif skip == "exported":
        nxt = "Waiting for AvaData results"
    elif contacts and not any(c["manual_allowed"] for c in contacts):
        nxt = "Review contact eligibility, then call"
    else:
        nxt = "Underwrite and promote to the pipeline"
    con.execute("""INSERT INTO lead_scores(property_id,score,tier,components,evidence,missing,confidence,calculated_at,next_action,disqualifiers)
                   VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(property_id) DO UPDATE SET score=excluded.score, tier=excluded.tier,
                   components=excluded.components, evidence=excluded.evidence, missing=excluded.missing, confidence=excluded.confidence,
                   calculated_at=excluded.calculated_at, next_action=excluded.next_action, disqualifiers=excluded.disqualifiers""",
                (pid, score, tier, _j(comps), _j(evidence), _j(sorted(set(missing))), conf, now(), nxt, _j(disq + review_reasons)))
    return {"score": score, "tier": tier}


# ---------------------------------------------------------------- queries for the app
def _lead_row(r):
    d = dict(r)
    for k in ("components", "evidence", "missing", "disqualifiers"):
        if k in d:
            try:
                d[k] = json.loads(d[k] or "null")
            except Exception:
                pass
    return d


def leads(con, f=None):
    f = f or {}
    where, args = ["1=1"], []
    if f.get("id"):
        where.append("p.id=?"); args.append(f["id"])
    if f.get("ids"):
        ids = [str(x) for x in f["ids"]][:1000]
        where.append(f"p.id IN ({','.join('?' * len(ids))})"); args += ids
    if f.get("county"):
        where.append("lower(p.county)=lower(?)"); args.append(f["county"])
    if f.get("state"):
        where.append("p.state=?"); args.append(f["state"].upper())
    if f.get("category"):
        where.append("EXISTS(SELECT 1 FROM lead_events e WHERE e.property_id=p.id AND e.category=?)"); args.append(f["category"])
    if f.get("tiers"):
        t = [x for x in f["tiers"] if x in ("A", "B", "C", "Review")]
        if t:
            where.append(f"s.tier IN ({','.join('?' * len(t))})"); args += t
    if f.get("min_score") not in (None, ""):
        where.append("s.score>=?"); args.append(int(f["min_score"]))
    if f.get("status"):
        where.append("h.status=?"); args.append(f["status"])
    if f.get("skip_status"):
        where.append("h.skip_status=?"); args.append(f["skip_status"])
    if f.get("multi"):
        where.append("(SELECT COUNT(DISTINCT category) FROM lead_events e WHERE e.property_id=p.id)>1")
    if f.get("q"):
        where.append("(p.street LIKE ? OR p.parcel_id LIKE ? OR EXISTS(SELECT 1 FROM lead_events e WHERE e.property_id=p.id AND e.case_ref LIKE ?))")
        args += [f"%{f['q']}%"] * 3
    if f.get("discovered_after"):
        where.append("h.first_seen>=?"); args.append(f["discovered_after"])
    if not f.get("include_dismissed"):
        where.append("h.status NOT IN ('dismissed')")
    order = {"score": "s.score DESC", "date": "next_date ASC", "new": "h.first_seen DESC"}.get(f.get("sort") or "score", "s.score DESC")
    lim = max(1, min(int(f.get("limit") or 200), 1000))
    off = max(0, int(f.get("offset") or 0))
    q = f"""SELECT p.*, s.score, s.tier, s.components, s.evidence, s.missing, s.confidence, s.next_action, s.disqualifiers, s.calculated_at,
             h.status AS lead_status, h.deal_id, h.skip_status, h.first_seen,
             (SELECT MIN(event_date) FROM lead_events e WHERE e.property_id=p.id AND e.event_date>=date('now')) AS next_date,
             (SELECT group_concat(DISTINCT category) FROM lead_events e WHERE e.property_id=p.id) AS categories,
             (SELECT COUNT(*) FROM contact_points c WHERE c.property_id=p.id AND c.suppressed=0) AS contacts,
             (SELECT name FROM owners o JOIN owner_property op ON op.owner_id=o.id WHERE op.property_id=p.id LIMIT 1) AS owner
             FROM properties p JOIN hunter_leads h ON h.property_id=p.id LEFT JOIN lead_scores s ON s.property_id=p.id
             WHERE {' AND '.join(where)} ORDER BY {order}, p.id LIMIT ? OFFSET ?"""
    rows = [_lead_row(r) for r in con.execute(q, (*args, lim, off))]
    total = con.execute(f"SELECT COUNT(*) FROM properties p JOIN hunter_leads h ON h.property_id=p.id LEFT JOIN lead_scores s ON s.property_id=p.id WHERE {' AND '.join(where)}", args).fetchone()[0]
    return {"rows": rows, "total": total}


def lead_detail(con, pid):
    r = leads(con, {"id": pid, "include_dismissed": True, "limit": 1})
    row = r["rows"][0] if r["rows"] else None
    if not row:
        p = con.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone()
        if not p:
            raise ValueError("Lead not found.")
        row = dict(p)
    row["events"] = [dict(e) for e in con.execute("SELECT * FROM lead_events WHERE property_id=? ORDER BY event_date DESC", (pid,))]
    row["owners"] = [dict(o) for o in con.execute("SELECT o.*, op.confidence AS match FROM owners o JOIN owner_property op ON op.owner_id=o.id WHERE op.property_id=?", (pid,))]
    row["contacts"] = [dict(c) for c in con.execute("SELECT * FROM contact_points WHERE property_id=? ORDER BY confidence, id", (pid,))]
    row["raw"] = [{"source": x["source_id"], "fetched_at": x["fetched_at"], "payload": json.loads(x["payload"])}
                  for x in con.execute("SELECT r.* FROM raw_records r JOIN lead_events e ON e.raw_id=r.id WHERE e.property_id=? LIMIT 5", (pid,))]
    row["sources"] = {s["id"]: s["name"] for s in con.execute("SELECT id,name FROM hunter_sources")}
    return row


def set_lead(con, pid, status=None, reason=None, deal_id=None):
    if status and status not in ("new", "review", "qualified", "promoted", "dismissed", "nurture"):
        raise ValueError("Unknown status.")
    sets, args = ["updated=?"], [now()]
    if status:
        sets.append("status=?"); args.append(status)
    if reason is not None:
        sets.append("reason=?"); args.append(str(reason)[:300])
    if deal_id is not None:
        sets.append("deal_id=?"); args.append(deal_id)
    con.execute(f"UPDATE hunter_leads SET {', '.join(sets)} WHERE property_id=?", (*args, pid))
    con.commit()
    score_property(con, pid)
    con.commit()
    return True


def summary(con):
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    by_cat = {r[0]: r[1] for r in con.execute("SELECT category, COUNT(DISTINCT property_id) FROM lead_events GROUP BY category")}
    by_county = {r[0]: r[1] for r in con.execute("SELECT county || ' ' || state, COUNT(*) FROM properties GROUP BY county, state")}
    tiers = {r[0] or "unscored": r[1] for r in con.execute("SELECT tier, COUNT(*) FROM lead_scores s JOIN hunter_leads h ON h.property_id=s.property_id WHERE h.status!='dismissed' GROUP BY tier")}
    return {
        "records_24h": q("SELECT COUNT(*) FROM raw_records WHERE fetched_at>=?", since),
        "new_properties_24h": q("SELECT COUNT(*) FROM hunter_leads WHERE first_seen>=?", since),
        "properties": q("SELECT COUNT(*) FROM properties"),
        "events": q("SELECT COUNT(*) FROM lead_events"),
        "by_category": by_cat, "by_county": by_county, "tiers": tiers,
        "review": q("SELECT COUNT(*) FROM lead_scores s JOIN hunter_leads h ON h.property_id=s.property_id WHERE s.tier='Review' AND h.status NOT IN ('dismissed','promoted')")
                  + q("SELECT COUNT(*) FROM review_queue WHERE resolved IS NULL"),
        "review_queue": q("SELECT COUNT(*) FROM review_queue WHERE resolved IS NULL"),
        "skip_pending": q("SELECT COUNT(*) FROM skip_batches WHERE status IN ('exported','submitted')"),
        "skip_never": q("SELECT COUNT(*) FROM hunter_leads WHERE skip_status='none' AND status NOT IN ('dismissed')"),
        "contacts": q("SELECT COUNT(*) FROM contact_points WHERE suppressed=0"),
        "promoted": q("SELECT COUNT(*) FROM hunter_leads WHERE status='promoted'"),
        "qualified": q("SELECT COUNT(*) FROM hunter_leads WHERE status='qualified'"),
        "jobs_running": q("SELECT COUNT(*) FROM hunter_jobs WHERE status IN ('queued','running')"),
        "jobs_failed_7d": q("SELECT COUNT(*) FROM hunter_jobs WHERE status='failed' AND requested>=?", (datetime.now() - timedelta(days=7)).isoformat()),
        "upcoming_7d": q("SELECT COUNT(DISTINCT property_id) FROM lead_events WHERE event_date BETWEEN date('now') AND date('now','+7 day')"),
    }


def jobs(con, limit=50):
    out = []
    for r in con.execute("SELECT j.*, s.name AS source_name FROM hunter_jobs j LEFT JOIN hunter_sources s ON s.id=j.source_id ORDER BY j.id DESC LIMIT ?", (limit,)):
        d = dict(r)
        try:
            d["errors"] = json.loads(d["errors"] or "[]")
        except Exception:
            pass
        out.append(d)
    return out


# ---------------------------------------------------------------- review queue
def add_review(con, kind, ref, payload, reason):
    con.execute("INSERT OR IGNORE INTO review_queue(kind,ref,payload,reason,created) VALUES(?,?,?,?,?)", (kind, ref, _j(payload), reason, now()))


def review_items(con, include_resolved=False):
    q = "SELECT * FROM review_queue" + ("" if include_resolved else " WHERE resolved IS NULL") + " ORDER BY id DESC LIMIT 300"
    out = []
    for r in con.execute(q):
        d = dict(r)
        d["payload"] = json.loads(d["payload"] or "{}")
        out.append(d)
    return out


def resolve_review(con, rid, action, property_id=None):
    r = con.execute("SELECT * FROM review_queue WHERE id=?", (rid,)).fetchone()
    if not r or r["resolved"]:
        raise ValueError("Already resolved or not found.")
    payload = json.loads(r["payload"] or "{}")
    if r["kind"] == "skip_row" and action == "attach":
        if not property_id:
            raise ValueError("Pick which property this contact belongs to.")
        _store_contacts(con, payload["contacts"], property_id, None, payload.get("batch_id"), "medium (you matched it)")
        score_property(con, property_id)
    con.execute("UPDATE review_queue SET resolved=?, resolution=? WHERE id=?", (now(), action + (f":{property_id}" if property_id else ""), rid))
    con.commit()
    return True


# ---------------------------------------------------------------- AvaData skip-trace export / import
# AvaData's exact upload columns could not be verified from public documentation, so the template is editable.
# These defaults use common skip-trace headers; match them to the column names AvaData shows when you upload.
DEFAULT_TEMPLATE = [
    {"header": "Property Address", "field": "street"}, {"header": "Property City", "field": "city"},
    {"header": "Property State", "field": "state"}, {"header": "Property Zip", "field": "zip"},
    {"header": "Owner First Name", "field": "owner_first"}, {"header": "Owner Last Name", "field": "owner_last"},
    {"header": "Owner Full Name", "field": "owner"},
    {"header": "Mailing Address", "field": "mail_street"}, {"header": "Mailing City", "field": "mail_city"},
    {"header": "Mailing State", "field": "mail_state"}, {"header": "Mailing Zip", "field": "mail_zip"},
    {"header": "Reference", "field": "row_ref"},
]
EXPORT_FIELDS = {"street", "city", "state", "zip", "county", "parcel_id", "owner", "owner_first", "owner_last",
                 "mail_street", "mail_city", "mail_state", "mail_zip", "row_ref"}


def template(con):
    try:
        t = json.loads(db.kv_get(con, "skip_template") or "null")
    except Exception:
        t = None
    return t or DEFAULT_TEMPLATE


def set_template(con, t):
    clean = [{"header": str(c["header"])[:60], "field": c["field"]} for c in t if c.get("header") and c.get("field") in EXPORT_FIELDS]
    if not clean or not any(c["field"] == "street" for c in clean):
        raise ValueError("The template needs at least the property address column.")
    db.kv_set(con, "skip_template", _j(clean))
    return clean


def _safe_cell(v):
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t") else s  # block spreadsheet formula injection


def _split_name(n, et):
    if not n or et != "person":
        return "", ""
    s = re.sub(r"\s+", " ", n.replace(",", ", ")).strip()
    if "," in n:  # LAST, FIRST
        last, first = [x.strip() for x in n.split(",", 1)]
        return first.split(" ")[0].title(), last.title()
    parts = s.split(" ")
    return (parts[0].title(), parts[-1].title()) if len(parts) > 1 else ("", parts[0].title())


def preview_batch(con, f):
    f = dict(f or {})
    f.setdefault("limit", 1000)
    rows = leads(con, f)["rows"]
    if f.get("skip_status_in"):
        rows = [r for r in rows if r.get("skip_status") in f["skip_status_in"]]
    if not f.get("include_with_contacts"):
        rows = [r for r in rows if not r.get("contacts")]
    seen, out, dropped = set(), [], {"duplicate": 0, "no_address": 0, "promoted": 0}
    for r in rows:
        if r.get("lead_status") == "promoted" and not f.get("include_promoted"):
            dropped["promoted"] += 1
            continue
        if not r.get("street") or not re.match(r"^\d", r["street"]):
            dropped["no_address"] += 1
            continue
        k = r.get("addr_key") or r["id"]
        if k in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(k)
        out.append(r)
    return {"rows": out, "dropped": dropped}


def create_batch(con, f, template_override=None):
    pv = preview_batch(con, f)
    if not pv["rows"]:
        raise ValueError("No leads match those filters (after removing duplicates, leads without a street address, and ones already skip-traced).")
    tpl = template_override or template(con)
    bid = "sb_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow([c["header"] for c in tpl])
    for i, r in enumerate(pv["rows"], 1):
        ref = f"{bid[-4:]}-{i:05d}"
        o = con.execute("SELECT o.* FROM owners o JOIN owner_property op ON op.owner_id=o.id WHERE op.property_id=? LIMIT 1", (r["id"],)).fetchone()
        o = dict(o) if o else {}
        first, last = _split_name(o.get("name"), o.get("entity_type"))
        ms, mc, mst, mz = split_street(o.get("mailing_address")) if o.get("mailing_address") else ("", "", "", "")
        vals = {"street": (r["street"] + (f" #{r['unit']}" if r.get("unit") else "")), "city": r.get("city"), "state": r.get("state"),
                "zip": r.get("zip"), "county": r.get("county"), "parcel_id": r.get("parcel_id"), "owner": o.get("name", ""),
                "owner_first": first, "owner_last": last, "mail_street": ms, "mail_city": mc, "mail_state": mst, "mail_zip": mz, "row_ref": ref}
        w.writerow([_safe_cell(vals.get(c["field"])) for c in tpl])
        con.execute("INSERT INTO skip_rows(batch_id,row_ref,property_id,owner_id,addr_key,exported) VALUES(?,?,?,?,?,?)",
                    (bid, ref, r["id"], o.get("id"), r.get("addr_key"), _j(vals)))
        con.execute("UPDATE hunter_leads SET skip_status='exported', updated=? WHERE property_id=?", (now(), r["id"]))
    text = "﻿" + buf.getvalue()  # UTF-8 BOM so Excel keeps accents
    from . import config
    out = config.path("out")
    out.mkdir(exist_ok=True)
    path = out / f"skiptrace_{bid}.csv"
    path.write_text(text, encoding="utf-8", newline="")
    man = out / f"skiptrace_{bid}_manifest.csv"
    with open(man, "w", encoding="utf-8", newline="") as fh:
        mw = csv.writer(fh)
        mw.writerow(["Reference", "Property ID", "Owner ID", "Address"])
        for r2 in con.execute("SELECT * FROM skip_rows WHERE batch_id=?", (bid,)):
            mw.writerow([r2["row_ref"], r2["property_id"], r2["owner_id"] or "", json.loads(r2["exported"])["street"]])
    con.execute("""INSERT INTO skip_batches(id,created,template,filters,selected,exported,status,file_path) VALUES(?,?,?,?,?,?,?,?)""",
                (bid, now(), _j(tpl), _j(f), len(pv["rows"]) + sum(pv["dropped"].values()), len(pv["rows"]), "exported", str(path)))
    con.commit()
    for r in pv["rows"]:
        score_property(con, r["id"])
    con.commit()
    return {"batch_id": bid, "rows": len(pv["rows"]), "dropped": pv["dropped"], "csv": text, "filename": path.name, "path": str(path), "manifest": str(man)}


def batches(con):
    out = []
    for r in con.execute("SELECT * FROM skip_batches ORDER BY created DESC LIMIT 100"):
        d = dict(r)
        for k in ("template", "filters", "file_hashes"):
            try:
                d[k] = json.loads(d[k] or "null")
            except Exception:
                pass
        out.append(d)
    return out


def batch_csv(con, bid):
    b = con.execute("SELECT file_path FROM skip_batches WHERE id=?", (bid,)).fetchone()
    if not b:
        raise ValueError("Batch not found.")
    from pathlib import Path
    p = Path(b["file_path"])
    return {"csv": p.read_text(encoding="utf-8") if p.exists() else "", "filename": p.name}


def mark_submitted(con, bid):
    con.execute("UPDATE skip_batches SET status='submitted', submitted_at=? WHERE id=? AND status='exported'", (now(), bid))
    con.commit()


def _norm_phone(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return "+1" + d if len(d) == 10 else ""


def _dnc_set():
    from . import config, sms
    try:
        cfg = config.load()
        return sms._dnc(cfg)
    except Exception:
        return set()


def _store_contacts(con, contacts, pid, oid, bid, confidence):
    dnc = _dnc_set()
    added = dup = 0
    for c in contacts:
        ph = _norm_phone(c.get("phone"))
        if not ph:
            continue
        key = f"{pid}|{ph}"
        sup = ph in dnc
        cur = con.execute("""INSERT OR IGNORE INTO contact_points(id,dedupe_key,property_id,owner_id,name,phone,phone_type,email,provider_status,
                             batch_id,returned_at,confidence,suppressed,suppressed_reason,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (_uid("c_"), key, pid, oid, c.get("name") or "", ph, c.get("type") or "", c.get("email") or "",
                           c.get("status") or "", bid, now(), confidence, 1 if sup else 0, "on Do Not Contact list" if sup else None, now()))
        if cur.rowcount:
            added += 1
        else:
            dup += 1
    return added, dup


IMPORT_FIELDS = ["ref", "street", "city", "state", "zip", "name", "first", "last", "email", "phones", "phone_types", "status"]


def import_results(con, bid, rows, mapping, file_hash):
    """rows: list of dicts (header -> value). mapping: our field -> header (phones/phone_types: list of headers).
    Matching: Reference column -> manifest; else address within this batch; else unique address anywhere (medium); else review/unmatched."""
    b = con.execute("SELECT * FROM skip_batches WHERE id=?", (bid,)).fetchone() if bid else None
    if bid and not b:
        raise ValueError("Batch not found.")
    if not mapping.get("phones") or not (mapping.get("street") or mapping.get("ref")):
        raise ValueError("Map at least one phone column and either the Reference or the property address column.")
    hashes = json.loads(b["file_hashes"] or "[]") if b else []
    repeat = file_hash in hashes
    manifest = {r["row_ref"]: dict(r) for r in con.execute("SELECT * FROM skip_rows WHERE batch_id=?", (bid,))} if bid else {}
    by_addr = {}
    for m in manifest.values():
        by_addr.setdefault(m["addr_key"], []).append(m)
    res = {"rows": len(rows), "matched": 0, "unmatched": 0, "review": 0, "failed": 0, "contacts_added": 0, "duplicates": 0,
           "repeat_file": repeat, "errors": []}
    get = lambda r, k: str(r.get(mapping.get(k) or "", "") or "").strip()
    for i, r in enumerate(rows, 1):
        try:
            phones = []
            types = mapping.get("phone_types") or []
            for j, h in enumerate(mapping.get("phones") or []):
                ph = _norm_phone(r.get(h))
                if ph:
                    phones.append({"phone": ph, "type": str(r.get(types[j], "") if j < len(types) else "").strip(),
                                   "name": (get(r, "name") or " ".join(x for x in [get(r, "first"), get(r, "last")] if x)).strip(),
                                   "email": get(r, "email"), "status": get(r, "status")})
            if not phones:
                res["failed"] += 1
                res["errors"].append({"row": i, "reason": "no valid phone number"})
                continue
            ref, st, z, city = get(r, "ref"), get(r, "street"), get(r, "zip"), get(r, "city")
            target, conf = None, None
            if ref and ref in manifest:
                target, conf = manifest[ref], "high"
            elif st:
                ak = addr_key(st, z, city)
                cands = by_addr.get(ak, [])
                if len(cands) == 1:
                    target, conf = cands[0], "high"
                elif len(cands) > 1:
                    add_review(con, "skip_row", f"{bid}:{file_hash}:{i}", {"batch_id": bid, "row": i, "address": st, "contacts": phones,
                               "candidates": [c["property_id"] for c in cands]}, "Address matches more than one exported lead")
                    res["review"] += 1
                    continue
                else:
                    glob = con.execute("SELECT id FROM properties WHERE addr_key=?", (ak,)).fetchall() if ak else []
                    if len(glob) == 1:
                        target, conf = {"property_id": glob[0]["id"], "owner_id": None}, "medium"
            if not target:
                add_review(con, "skip_row", f"{bid}:{file_hash}:{i}", {"batch_id": bid, "row": i, "address": st or ref, "contacts": phones,
                           "candidates": []}, "Couldn't match this row to a lead")
                res["unmatched"] += 1
                continue
            # name check: if we know the owner and the returned name shares no surname token, don't attach automatically
            known = con.execute("SELECT o.name_key FROM owners o JOIN owner_property op ON op.owner_id=o.id WHERE op.property_id=?", (target["property_id"],)).fetchall()
            ret = name_key(phones[0]["name"])
            if known and ret and not any(set(ret.split()) & set(k["name_key"].split()) for k in known):
                add_review(con, "skip_row", f"{bid}:{file_hash}:{i}", {"batch_id": bid, "row": i, "address": st or ref, "contacts": phones,
                           "candidates": [target["property_id"]], "returned_name": phones[0]["name"]},
                           "Returned name doesn't match the owner on record")
                res["review"] += 1
                continue
            a, d = _store_contacts(con, phones, target["property_id"], target.get("owner_id"), bid, conf)
            res["contacts_added"] += a
            res["duplicates"] += d
            res["matched"] += 1
            con.execute("UPDATE hunter_leads SET skip_status='traced', updated=? WHERE property_id=?", (now(), target["property_id"]))
            score_property(con, target["property_id"])
        except Exception as e:
            res["failed"] += 1
            res["errors"].append({"row": i, "reason": f"{e.__class__.__name__}: {e}"})
    if b:
        if not repeat:
            hashes.append(file_hash)
        con.execute("""UPDATE skip_batches SET status='imported', returned_at=?, imported=imported+?, matched=matched+?, unmatched=unmatched+?,
                       duplicates=duplicates+?, failed=failed+?, review=review+?, file_hashes=? WHERE id=?""",
                    (now(), 0 if repeat else res["rows"], res["matched"] if not repeat else 0, res["unmatched"] if not repeat else 0,
                     res["duplicates"], res["failed"] if not repeat else 0, res["review"] if not repeat else 0, _j(hashes), bid))
        # leads in the batch that came back with nothing
        if not repeat:
            con.execute("""UPDATE hunter_leads SET skip_status='no_result' WHERE skip_status='exported' AND property_id IN
                           (SELECT property_id FROM skip_rows WHERE batch_id=?)""", (bid,))
    con.commit()
    res["errors"] = res["errors"][:100]
    return res


CONTACT_FLAGS = ["identity_verified", "consent_reviewed", "dnc_screened", "sms_reviewed", "manual_allowed", "auto_approved", "suppressed"]


def set_contact(con, cid, flags):
    c = con.execute("SELECT * FROM contact_points WHERE id=?", (cid,)).fetchone()
    if not c:
        raise ValueError("Contact not found.")
    upd = {k: 1 if v else 0 for k, v in (flags or {}).items() if k in CONTACT_FLAGS}
    if upd.get("auto_approved") and not (upd.get("consent_reviewed", c["consent_reviewed"]) and upd.get("dnc_screened", c["dnc_screened"])):
        raise ValueError("Automated outreach needs consent reviewed and DNC screening done first.")
    if c["suppressed"] and not upd.get("suppressed", 1) and c["phone"] in _dnc_set():
        raise ValueError("This number is on your Do Not Contact list. Remove it there first.")
    if upd.get("suppressed"):
        upd.update(manual_allowed=0, auto_approved=0, sms_reviewed=0)
    if not upd:
        return False
    upd["updated"] = now()
    con.execute(f"UPDATE contact_points SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), cid))
    con.commit()
    return True


def suppress_phone(con, phone, reason):
    """Opt-outs propagate: every contact point with this number is suppressed (Hunter and Closer share the DNC list)."""
    ph = _norm_phone(phone)
    if not ph:
        return 0
    cur = con.execute("UPDATE contact_points SET suppressed=1, suppressed_reason=?, manual_allowed=0, auto_approved=0, sms_reviewed=0, updated=? WHERE phone=?",
                      (reason, now(), ph))
    con.commit()
    return cur.rowcount


# ---------------------------------------------------------------- manual uploads
def ingest_manual(con, state, county, category, rows, filename=""):
    if category not in CATEGORIES:
        raise ValueError("Unknown category.")
    if not county or not state:
        raise ValueError("County and state are required.")
    sid = ensure_manual_source(con, state.upper()[:2], county, category)
    src = dict(con.execute("SELECT * FROM hunter_sources WHERE id=?", (sid,)).fetchone())
    cur = con.execute("INSERT INTO hunter_jobs(source_id,kind,requested,started,status,params) VALUES(?,?,?,?,?,?)",
                      (sid, "manual_upload", now(), now(), "running", _j({"file": filename[:120]})))
    jid = cur.lastrowid
    recs = [dict(r, category=category, county=r.get("county") or county, state=r.get("state") or state) for r in rows]
    stats = ingest(con, src, recs, jid)
    con.execute("UPDATE hunter_jobs SET status='done', finished=?, fetched=?, accepted=?, new_events=?, duplicates=?, rejected=?, errors=? WHERE id=?",
                (now(), stats["fetched"], stats["accepted"], stats["new_events"], stats["duplicates"], stats["rejected"], _j(stats["errors"][:50]), jid))
    con.execute("UPDATE hunter_sources SET last_success=?, last_attempt=? WHERE id=?", (now(), now(), sid))
    con.commit()
    return dict(stats, source_id=sid, errors=stats["errors"][:50])


# ---------------------------------------------------------------- dispatcher used by the MCP bridge
READ_OPS = {"summary", "leads", "lead", "coverage", "jobs", "batches", "batch_csv", "review", "weights", "template"}
WRITE_OPS = {"run_source", "cancel_job", "set_source", "set_lead", "create_batch", "mark_submitted", "import_results",
             "resolve_review", "set_contact", "set_weights", "set_template", "ingest_manual", "rescore"}


def handle(con, cfg, req):
    """One entry point for the app. req = {op, ...}. Returns plain data."""
    op = (req or {}).get("op")
    a = req or {}
    if op == "summary":
        return summary(con)
    if op == "leads":
        return leads(con, a.get("filters") or {})
    if op == "lead":
        return lead_detail(con, a["id"])
    if op == "coverage":
        return coverage(con, cfg)
    if op == "jobs":
        return {"jobs": jobs(con, int(a.get("limit") or 50))}
    if op == "batches":
        return {"batches": batches(con), "template": template(con)}
    if op == "batch_csv":
        return batch_csv(con, a["batch_id"])
    if op == "review":
        return {"items": review_items(con, bool(a.get("include_resolved")))}
    if op == "weights":
        w = weights(con)
        return {"weights": {k: w[k] for k in DEFAULT_WEIGHTS}, "tiers": w["_tiers"], "defaults": DEFAULT_WEIGHTS}
    if op == "template":
        return {"template": template(con), "fields": sorted(EXPORT_FIELDS), "default": DEFAULT_TEMPLATE}
    if op == "run_source":
        return {"job_id": request_job(con, a["source_id"], "manual", a.get("params") or {})}
    if op == "cancel_job":
        cancel_job(con, int(a["job_id"]))
        return {"ok": True}
    if op == "set_source":
        return {"ok": set_source(con, a["source_id"], **(a.get("fields") or {}))}
    if op == "set_lead":
        return {"ok": set_lead(con, a["id"], a.get("status"), a.get("reason"), a.get("deal_id"))}
    if op == "create_batch":
        return create_batch(con, a.get("filters") or {})
    if op == "mark_submitted":
        mark_submitted(con, a["batch_id"])
        return {"ok": True}
    if op == "import_results":
        return import_results(con, a.get("batch_id"), a.get("rows") or [], a.get("mapping") or {}, a.get("file_hash") or "")
    if op == "resolve_review":
        return {"ok": resolve_review(con, int(a["id"]), a.get("action") or "dismiss", a.get("property_id"))}
    if op == "set_contact":
        return {"ok": set_contact(con, a["id"], a.get("flags") or {})}
    if op == "set_weights":
        return {"rescored": set_weights(con, a.get("weights"), a.get("tiers"))}
    if op == "set_template":
        return {"template": set_template(con, a.get("template") or [])}
    if op == "ingest_manual":
        return ingest_manual(con, a.get("state") or "", a.get("county") or "", a.get("category") or "", a.get("rows") or [], a.get("filename") or "")
    if op == "rescore":
        return {"rescored": set_weights(con)}
    raise ValueError(f"Unknown Hunter operation '{op}'.")
