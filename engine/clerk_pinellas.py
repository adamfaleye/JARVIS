"""Pinellas County Clerk public data files -> The Hunter (pre-foreclosure, probate, evictions).

The Clerk posts free daily CSV exports at publicfiles.mypinellasclerk.gov/download/ (no login, no CAPTCHA):
  CIVIL/NEW_CASE_FILINGS_DAILY/CivilNewFilingsDaily_MM-DD-YYYY.csv        every party on every new civil case, with addresses
  PROBATE/NEW_ESTATE_CASE_FILINGS_DAILY/EstateNewCaseFilingsDaily_*.csv   new estates: decedent, petitioner, attorney

From those:
  preforeclosure  new residential mortgage foreclosure cases (homestead / non-homestead; commercial and land are left out).
                  Property = the "UNKNOWN TENANT" address (served at the property) or a defendant's own Pinellas address,
                  confirmed on the property appraiser's site; the owner of record comes from the appraiser.
  probate         new estates. The decedent's name is searched on the appraiser's site; a parcel is kept only when the owner
                  of record is that person (last + first name, middle initial agreeing) and the match isn't ambiguous.
  eviction        new residential evictions / unlawful detainers ("tired landlord"). The rental is the tenant's address,
                  confirmed on the appraiser's site; institutional landlords and mobile-home lots are left out.
                  Tenant names are not stored.

Nothing is guessed: a case whose property can't be confirmed is skipped (and counted), never filled in. Files are kept in
data/clerk/ and appraiser answers are cached, so re-reading the last 30 days each day only costs the new cases.
"""
import csv
import io
import json
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta

from . import config, db
from . import owner_lookup as ol

BASE = "https://publicfiles.mypinellasclerk.gov/download/"
FEEDS = {
    "civil": ("CIVIL/NEW_CASE_FILINGS_DAILY/", re.compile(r"CivilNewFilingsDaily_(\d\d)-(\d\d)-(\d{4})\.csv$")),
    "estate": ("PROBATE/NEW_ESTATE_CASE_FILINGS_DAILY/", re.compile(r"EstateNewCaseFilingsDaily_(\d\d)-(\d\d)-(\d{4})\.csv$")),
}
FEED_FOR = {"preforeclosure": "civil", "eviction": "civil", "probate": "estate"}
LABEL = {"preforeclosure": "Pinellas Clerk: new mortgage foreclosure filings (lis pendens)",
         "probate": "Pinellas Clerk: new estate (probate) filings",
         "eviction": "Pinellas Clerk: new residential eviction filings"}

RES_FRCL = re.compile(r"MTGE FRCL (?:HOMESTEAD|NON-HOMESTEAD) RES")
EVICTION = re.compile(r"^(?:RESIDENTIAL EVICTION|UNLAWFUL DETAINER)")
PLACEHOLDER = re.compile(r"\bUNKNOWN\b|\bANY AND ALL\b|\bALL OTHER\b|\bIN POSSESSION\b|\bOCCUPANTS?\b|\bTENANTS?\b|\bJOHN DOE\b|\bJANE DOE\b")
ENTITY = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO|BANK|N\.?A|ASSOCIATION|ASSN|TRUST|TRUSTEE|LP|LLLP|LTD|PLLC|PA|"
                    r"FUND|HOLDINGS|PARTNERS|CONDOMINIUM|HOMEOWNERS|COUNTY|CITY|STATE|DEPARTMENT|SECRETARY|UNITED STATES|AUTHORITY|"
                    r"SERVICING|MORTGAGE|FINANCIAL|CREDIT UNION|INVESTMENTS?|PROPERTIES|ESTATE OF)\b")
INSTITUTIONAL = re.compile(r"\b(BORROWER|FUND|REIT|APARTMENTS?|COMMUNITIES|HOUSING AUTHORITY|MHC|LP|LLLP|SFR|RENEWAL|PROGRESS RESIDENTIAL|"
                           r"INVITATION|AMERICAN HOMES|FIRSTKEY|HOME PARTNERS|TRICON|PROPERTY OWNER)\b")
BIG_USE = re.compile(r"APARTMENT|MOBILE HOME PARK|10 UNITS|COMMERCIAL|OFFICE|STORE|INDUSTRIAL|HOTEL|MOTEL", re.I)
NOT_A_HOME = re.compile(r"SLIP|PARKING|STORAGE|COMMON AREA|CABANA|BOAT|GARAGE UNIT|RIGHT.OF.WAY|SUBMERGED", re.I)
MAX_OWNERS = 5
NAME_SUFFIX = {"EST", "ESTATE", "TRE", "TR", "TRS", "TRUSTEE", "JR", "SR", "II", "III", "IV", "ET", "AL", "ETAL", "LE", "DECEASED"}


# ------------------------------------------------------------------ small helpers
def _sq(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().upper()


def _pinellas_zip(z):
    try:
        n = int(str(z)[:5])
    except ValueError:
        return False
    return 33701 <= n <= 33786 or 34677 <= n <= 34698


def split_addr(a):
    """'100 SAMPLE DR, CLEARWATER FL 33755' -> (street, city, state, zip). Street must start with a house number."""
    s = _sq(re.sub(r"\([^)]*\)", "", str(a or "")))
    if not s or s.startswith("C/O") or "NO ADDRESS" in s or "," not in s:
        return None
    street, rest = s.split(",", 1)
    street = street.strip()
    m = re.search(r"^(.*?)\s*\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", rest.strip())
    if not m or not re.match(r"^\d+[A-Z]?\s+\S", street):
        return None
    return street, m.group(1).strip(" ,"), m.group(2), m.group(3)


def _amount_band(case_type):
    m = re.search(r"(\$[\d,]+(?:\.\d+)?\s*-\s*\$[\d,]+(?:\.\d+)?|\$[\d,]+ OR MORE)", case_type or "")
    return m.group(1).replace(" - ", "–") if m else ""


def parse_owner_name(component):
    """PCPAO owner component 'DOE, JOHN P EST' -> (last, first, middle_initial, is_estate)."""
    c = _sq(component).replace(".", "")
    is_est = bool(re.search(r"\bEST\b|\bESTATE\b|\bDECEASED\b", c))
    if "," in c:
        last, rest = c.split(",", 1)
    else:
        parts = c.split()
        last, rest = (parts[0], " ".join(parts[1:])) if parts else ("", "")
    toks = [t for t in re.findall(r"[A-Z'-]+", rest) if t not in NAME_SUFFIX]
    return last.strip(), (toks[0] if toks else ""), (toks[1][0] if len(toks) > 1 else ""), is_est


def _nk(s):
    return re.sub(r"[^A-Z]", "", _sq(s))


def name_matches(component, first, middle, last):
    """Same person: last and first names equal (ignoring spaces/punctuation); middle initials agree when both are known."""
    l2, f2, m2, _ = parse_owner_name(component)
    if not last or not first or _nk(l2) != _nk(last) or _nk(f2) != _nk(first):
        return False
    return not (middle and m2 and middle[0].upper() != m2)


def _owners_match_people(owners, people, entities):
    """Is a defendant / plaintiff (person or company) on the appraiser's title?"""
    for o in owners:
        for (f, m, l) in people:
            if name_matches(o, f, m, l):
                return True
        for e in entities:
            if _nk(re.sub(r"\b(LLC|INC|CORP|CO|LTD)\b", "", _sq(e))) and \
                    _nk(re.sub(r"\b(LLC|INC|CORP|CO|LTD)\b", "", _sq(e))) == _nk(re.sub(r"\b(LLC|INC|CORP|CO|LTD)\b", "", _sq(o))):
                return True
    return False


# ------------------------------------------------------------------ network (paced, cached)
class Net:
    def __init__(self, fetch=None, pause_s=1.5, max_lookups=250, con=None, log=print):
        self.fetch = fetch or ol._get
        self.pause_s, self.max_lookups, self.lookups, self.con, self.log = pause_s, max_lookups, 0, con, log
        self._last = 0.0

    def _pace(self):
        wait = self.pause_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def get(self, url, **kw):
        self._pace()
        return self.fetch(url, **kw)

    # cache of appraiser answers, so the same case isn't looked up again tomorrow
    def cached(self, key, ttl_days):
        if self.con is None:
            return None
        r = self.con.execute("SELECT at, value FROM clerk_cache WHERE key=?", (key,)).fetchone()
        if r and r["at"] >= (datetime.now() - timedelta(days=ttl_days)).isoformat():
            return json.loads(r["value"])
        return None

    def store(self, key, value):
        if self.con is not None:
            self.con.execute("INSERT OR REPLACE INTO clerk_cache(key, at, value) VALUES(?,?,?)", (key, db.now(), json.dumps(value)))
            self.con.commit()

    def budget_left(self):
        return self.lookups < self.max_lookups


def _ensure_cache(con):
    con.execute("CREATE TABLE IF NOT EXISTS clerk_cache(key TEXT PRIMARY KEY, at TEXT, value TEXT)")
    con.commit()


def list_files(feed, net):
    """[(file_date, name)] newest first, from the Clerk's directory listing."""
    folder, pat = FEEDS[feed]
    page = net.get(BASE + folder)
    out = []
    for name in re.findall(r'href="\./([^"]+)"', page):
        name = urllib.parse.unquote(name)
        m = pat.search(name)
        if m:
            mm, dd, yy = m.groups()
            try:
                out.append((date(int(yy), int(mm), int(dd)), name))
            except ValueError:
                pass
    return sorted(set(out), reverse=True)


def read_file(feed, name, net, log=print):
    """A day's CSV, from data/clerk/ when already downloaded (the Clerk's files don't change once posted)."""
    folder, _ = FEEDS[feed]
    d = config.ROOT / "data" / "clerk" / feed
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if p.exists() and p.stat().st_size > 0:
        text = p.read_text(encoding="utf-8")
    else:
        text = net.get(BASE + folder + urllib.parse.quote(name))
        if not text.lstrip("﻿").startswith(("\"Case Category\"", "Case Category")):
            raise ValueError(f"{name}: not the expected CSV")
        p.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def _detail(net, strap):
    key = "pcpao:s:" + strap
    hit = net.cached(key, 30)
    if hit is not None:
        return hit or None
    if not net.budget_left():
        raise _OutOfBudget()
    net.lookups += 1
    page = net.get(ol.PINELLAS + "/property-details?s=" + urllib.parse.quote(strap))
    info = ol.parse_pinellas(page)
    if info["owners"]:
        info["source"] = "Pinellas County Property Appraiser (pcpao.gov)"
        info["source_url"] = ol.PINELLAS + "/property-details?s=" + strap
    net.store(key, info if info["owners"] else {})
    return info if info["owners"] else None


def _search(net, text, option):
    key = f"pcpao:{option}:{_sq(text)}"
    hit = net.cached(key, 14)
    if hit is not None:
        return hit
    if not net.budget_left():
        raise _OutOfBudget()
    net.lookups += 1
    raw = net.get(ol.PINELLAS + "/dal/quicksearch/homeSearch", data=urllib.parse.urlencode({"input": text, "option": option}).encode(),
                  headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    try:
        hits = json.loads(raw) or []
    except ValueError:
        hits = []
    net.store(key, hits)
    return hits


class _OutOfBudget(Exception):
    pass


def parcel_at(net, street):
    """The one parcel at this exact street address (unit included), with its appraiser record; None if not exactly one."""
    q = re.sub(r"\b(APT|UNIT|STE|SUITE|NO|LOT)\b\.?\s*", "", re.sub(r"#\s*", "", _sq(street))).strip()
    hits = _search(net, q, "address")
    want = ol._norm_site(street)
    exact = [h for h in hits if ol._norm_site(h.get("sa")) == want]
    if len({h.get("s") for h in exact}) != 1:
        return None
    info = _detail(net, exact[0]["s"])
    if info:
        info = dict(info, parcel_number=exact[0].get("pn") or "")
    return info


# ------------------------------------------------------------------ parsing the Clerk's files
def _cases(rows):
    out = {}
    for r in rows:
        out.setdefault(r.get("Case Number") or "", []).append(r)
    out.pop("", None)
    return out


def _party(r):
    first, mid, last = _sq(r.get("Party First Name")), _sq(r.get("Party Middle Name")), _sq(r.get("Party Last Name/Company Name"))
    full = _sq(" ".join(x for x in (first, mid, last) if x))
    kind = "placeholder" if PLACEHOLDER.search(full) else ("person" if first and not ENTITY.search(last) else "entity")
    return {"type": _sq(r.get("Party Type")), "first": first, "middle": mid, "last": last, "name": full, "kind": kind,
            "addr": split_addr(r.get("Party Address"))}


def _iso(d):
    d = (d or "").strip()
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}$", d):
        return datetime.strptime(d, "%m/%d/%Y").date().isoformat()
    return d[:10] if re.match(r"\d{4}-\d\d-\d\d", d) else ""


def _property_record(category, case, filed, info, street_src, notes, url=""):
    """One Hunter record per owner on title (the Hunter links them all to the same case)."""
    st, city, state, zip_ = street_src
    base = {"category": category, "county": "Pinellas", "state": "FL", "case_number": case, "event_date": filed,
            "address": st, "city": city.title(), "zip": zip_, "notes": notes[:500], "url": url}
    if info:
        base.update({"parcel_id": info.get("parcel_number") or "", "mailing_address": info.get("mailing_address") or "",
                     "assessed_value": info.get("just_value"), "property_type": info.get("use") or "",
                     "year_built": info.get("year_built"), "sqft": info.get("living_sqft"),
                     "_est_value": info.get("sales_comparison"), "url": info.get("source_url") or url})
        owners = info.get("owners") or []
        if len(owners) > MAX_OWNERS:  # heirs' property can list 20 names; the first ones on title are enough to reach the family
            base["notes"] = notes[:440] + f" {len(owners)} owners on title; first {MAX_OWNERS} kept."
            owners = owners[:MAX_OWNERS]
    else:
        owners = []
    return [dict(base, owner=o) for o in owners] or [base]


def foreclosures(rows, net, log=print, stats=None):
    """New residential mortgage foreclosure cases -> pre-foreclosure records."""
    stats = stats if stats is not None else {}
    out = []
    for case, parts in _cases(rows).items():
        ctype = _sq(parts[0].get("Case Type"))
        if not RES_FRCL.search(ctype):
            continue
        stats["cases"] = stats.get("cases", 0) + 1
        ps = [_party(r) for r in parts]
        plaintiffs = sorted({p["name"] for p in ps if p["type"] == "PLAINTIFF"})
        defs = [p for p in ps if p["type"] == "DEFENDANT"]
        people = [(p["first"], p["middle"], p["last"]) for p in defs if p["kind"] == "person"]
        companies = [p["last"] for p in defs if p["kind"] == "entity"]
        tenant_addrs = [p["addr"] for p in defs if p["kind"] == "placeholder" and p["addr"]]
        own_addrs = [p["addr"] for p in defs if p["kind"] in ("person", "entity") and p["addr"] and p["addr"][2] == "FL" and _pinellas_zip(p["addr"][3])]
        filed = _iso(parts[0].get("Case Create Date"))
        band = _amount_band(ctype)
        notes = (f"Mortgage foreclosure filed {filed} by {', '.join(plaintiffs) or 'unknown plaintiff'} "
                 f"({'homestead' if 'NON-HOMESTEAD' not in ctype else 'non-homestead'}{', claim ' + band if band else ''}). "
                 f"Defendants: {', '.join(p['name'] for p in defs if p['kind'] != 'placeholder')[:220]}. Pinellas Clerk case {case}.")
        found = None
        tried = []
        for a in tenant_addrs + own_addrs:  # the property itself first
            k = ol._norm_site(a[0])
            if k in tried:
                continue
            tried.append(k)
            info = parcel_at(net, a[0])
            at_property = a in tenant_addrs
            if info and (at_property or _owners_match_people(info["owners"], people, companies)):
                found = (a, info)
                break
            if not info and at_property:
                found = (a, None)  # served at this address but the appraiser search didn't confirm it: keep, unverified
        if not found:
            stats["no_property"] = stats.get("no_property", 0) + 1
            continue
        a, info = found
        if info is None:
            recs = _property_record("preforeclosure", case, filed, None, a, notes + " Address from service on the tenant; not confirmed on the appraiser's site.")
            recs = [dict(recs[0], owner=f"{l}, {f}{' ' + m if m else ''}") for (f, m, l) in people] or recs
        else:
            on_title = _owners_match_people(info["owners"], people, companies)
            recs = _property_record("preforeclosure", case, filed, info, a,
                                    notes + ("" if on_title else " Note: the owner of record isn't among the named defendants."))
        stats["records"] = stats.get("records", 0) + 1
        out += recs
    return out


def evictions(rows, net, log=print, stats=None):
    """New residential eviction cases -> tired-landlord records (the rental, its owner of record). No tenant names kept."""
    stats = stats if stats is not None else {}
    out = []
    for case, parts in _cases(rows).items():
        ctype = _sq(parts[0].get("Case Type"))
        if not EVICTION.search(ctype):
            continue
        stats["cases"] = stats.get("cases", 0) + 1
        ps = [_party(r) for r in parts]
        plaintiffs = [p for p in ps if p["type"] == "PLAINTIFF"]
        if any(INSTITUTIONAL.search(p["name"]) for p in plaintiffs):
            stats["institutional"] = stats.get("institutional", 0) + 1
            continue
        addrs = []
        for p in ps:
            if p["type"] == "DEFENDANT" and p["addr"] and p["addr"][2] == "FL" and not re.search(r"\bLOT\b", p["addr"][0]):
                if p["addr"] not in addrs:
                    addrs.append(p["addr"])
        info, a = None, None
        for cand in addrs[:2]:
            info = parcel_at(net, cand[0])
            if info:
                a = cand
                break
        if not info:
            stats["no_property"] = stats.get("no_property", 0) + 1
            continue
        if any(INSTITUTIONAL.search(o) for o in info["owners"]) or BIG_USE.search(info.get("use") or "") or NOT_A_HOME.search(info.get("use") or ""):
            stats["institutional"] = stats.get("institutional", 0) + 1
            continue
        filed = _iso(parts[0].get("Case Create Date"))
        self_filed = _owners_match_people(info["owners"], [(p["first"], p["middle"], p["last"]) for p in plaintiffs if p["kind"] == "person"],
                                          [p["last"] for p in plaintiffs if p["kind"] == "entity"])
        kind = "unlawful detainer" if ctype.startswith("UNLAWFUL") else "eviction"
        notes = (f"Residential {kind} filed {filed} by {', '.join(p['name'] for p in plaintiffs) or 'unknown'}"
                 f"{' (the owner filed it)' if self_filed else ' (filed by someone other than the owner of record, e.g. a manager)'}. "
                 f"Pinellas Clerk case {case}.")
        stats["records"] = stats.get("records", 0) + 1
        out += _property_record("eviction", case, filed, info, a, notes)
    return out


def estates(rows, net, log=print, stats=None):
    """New estate cases -> probate records, only where the decedent is clearly the owner of record of a Pinellas parcel."""
    stats = stats if stats is not None else {}
    out = []
    for case, parts in _cases(rows).items():
        r0 = parts[0]
        first, mid, last = _sq(r0.get("Decedent's First Name")), _sq(r0.get("Decedent's Middle Name")), _sq(r0.get("Decedent's Last Name"))
        if not first or not last:
            continue
        stats["cases"] = stats.get("cases", 0) + 1
        hits = _search(net, f"{last} {first}", "owner")
        reps, rep_names = [], []
        for r in parts:
            n = _sq(" ".join(x for x in (r.get("Rep or Petitioner First Name"), r.get("Rep or Petitioner's Middle Name"), r.get("Rep or Petitioner's Last Name")) if x))
            if n and n not in reps:
                reps.append(n)
            if r.get("Rep or Petitioner First Name") and r.get("Rep or Petitioner's Last Name"):
                rep_names.append((_sq(r.get("Rep or Petitioner First Name")), _sq(r.get("Rep or Petitioner's Last Name"))))
        # how sure each parcel is theirs: 3 marked as an estate, 2 the petitioner is a co-owner, 1 full middle name agrees, 0 name only
        matched = []
        for h in hits:
            comps = [c for c in str(h.get("name") or "").split(" / ") if c.strip()]
            for i, c in enumerate(comps):
                if name_matches(c, first, mid, last):
                    l2, f2, m2, est = parse_owner_name(c)
                    toks = [t for t in re.findall(r"[A-Z'-]+", _sq(c).split(",", 1)[-1]) if t not in NAME_SUFFIX]
                    co = any(name_matches(o, rf, "", rl) for j, o in enumerate(comps) if j != i for rf, rl in rep_names)
                    tier = 3 if est else 2 if co else 1 if (mid and len(toks) > 1 and _nk(toks[1]) == _nk(mid.split()[0])) else 0
                    matched.append((h, tier))
                    break
        if not matched:
            stats["no_property"] = stats.get("no_property", 0) + 1
            continue
        best = max(t for _, t in matched)
        use = [(h, t) for h, t in matched if t == best]
        if best < 2 and len({h["s"] for h, _ in use}) > 2:
            stats["ambiguous"] = stats.get("ambiguous", 0) + 1  # a common name: too many parcels to be sure which are theirs
            continue
        filed = _iso(r0.get("Case Create Date"))
        atty = _sq(" ".join(x for x in (r0.get("Rep or Petitioner Attorney's First Name"), r0.get("Rep or Petitioner Attorney's Middle Name"),
                                       r0.get("Rep or Petitioner Attorney 's Last Name")) if x))
        dod = _iso(r0.get("Decedent's Date of Death") or "")
        decedent = _sq(" ".join(x for x in (first, mid, last) if x))
        n_ok = 0
        for h, tier in use[:3]:
            info = _detail(net, h["s"])
            if not info or NOT_A_HOME.search(info.get("use") or ""):
                continue
            info = dict(info, parcel_number=h.get("pn") or "")
            st = split_addr(f"{h.get('sa')}, {(info.get('site_city_line') or '').replace(',', '')}") or (_sq(h.get("sa")), "", "FL", "")
            on_mail = any(re.search(r"\b%s\b" % re.escape(n.split()[-1]), _sq(info.get("mailing_address"))) for n in reps if n.split())
            conf = {3: "owner of record is marked as an estate", 2: "the petitioner is a co-owner on title",
                    1: "full name (with middle name) matches the owner of record"}.get(tier, "name match on the owner of record; confirm it's the same person")
            if on_mail and tier < 3:
                conf += "; tax bills go to the petitioner"
            notes = (f"Estate of {decedent}{', died ' + dod if dod else ''}: {_sq(r0.get('Case Type')).lower()} filed {filed}. "
                     f"Petitioner / personal rep: {', '.join(reps) or 'not listed'}{'; attorney ' + atty if atty else ''}. "
                     f"Parcel match: {conf}. Pinellas Clerk case {case}.")
            # one estate can own several parcels: each is its own lead (case + parcel number)
            out += _property_record("probate", f"{case}/{info['parcel_number'] or h['s']}", filed, info, st, notes)
            n_ok += 1
        stats["records"] = stats.get("records", 0) + n_ok
    return out


PARSERS = {"preforeclosure": foreclosures, "eviction": evictions, "probate": estates}


# ------------------------------------------------------------------ the adapter entry point
def fetch_records(category, days_back=30, net=None, log=print, should_stop=lambda: False, stats=None):
    """Records for one category from the last `days_back` days of Clerk files, newest first, until the lookup budget runs out."""
    stats = stats if stats is not None else {}
    feed = FEED_FOR[category]
    since = date.today() - timedelta(days=int(days_back))
    out = []
    files = [(d, n) for d, n in list_files(feed, net) if d >= since]
    stats.update(files=len(files), files_done=0)
    for d, name in files:
        if should_stop():
            break
        try:
            rows = read_file(feed, name, net, log)
            out += PARSERS[category](rows, net, log, stats)
            stats["files_done"] += 1
        except _OutOfBudget:
            stats["deferred"] = True  # the rest waits for tomorrow's run (answers so far are cached)
            break
        except Exception as e:  # noqa: BLE001  one bad or missing file never stops the rest
            stats.setdefault("file_errors", []).append(f"{name}: {e.__class__.__name__}: {str(e)[:120]}")
            if len(stats["file_errors"]) >= 3:
                raise RuntimeError("Clerk files keep failing: " + "; ".join(stats["file_errors"]))
    return out


def ensure_source(hc, category):
    sid = f"fl-pinellas-clerk-{category}"
    if not hc.execute("SELECT id FROM hunter_sources WHERE id=?", (sid,)).fetchone():
        from . import hunter
        hc.execute("""INSERT INTO hunter_sources(id,state,county,name,category,url,access_method,record_types,fields,adapter,config,enabled,
                      coverage_status,freshness_days,limitations,rate_limit_s,tos_status,created,schedule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (sid, "FL", "Pinellas", LABEL[category], category, BASE + FEEDS[FEED_FOR[category]][0], "bulk_download", hunter._j([category]),
                    hunter._j(["case_number", "address", "owner", "mailing_address", "event_date", "parcel_id"]), "clerk_pinellas",
                    hunter._j({"adapter": "clerk_pinellas", "category": category}), 1, "configured_untested", 1,
                    {"preforeclosure": "Residential mortgage foreclosures only. No loan balance in the file (claim range only).",
                     "probate": "Only estates whose decedent is clearly the owner of record of a Pinellas parcel. Petitioner has no address in the file.",
                     "eviction": "Owner-occupied institutional landlords and mobile-home lots left out. Tenant names not stored."}[category],
                    1.5, "Clerk's free public data files; no login; paced requests", hunter.now(), "daily"))
        hc.commit()
    return sid


def sync(cfg, categories=None, net_fetch=None, log=print, should_stop=lambda: False):
    """The daily run: each category as a Hunter job (with provenance), then property facts and rescoring."""
    from . import hunter
    conf = cfg.get("clerk_records") or {}
    cats = categories or conf.get("categories") or ["preforeclosure", "probate", "eviction"]
    mcon = db.connect()
    _ensure_cache(mcon)
    net = Net(fetch=net_fetch, pause_s=float(conf.get("pause_s", 1.5)), max_lookups=int(conf.get("max_lookups", 250)), con=mcon, log=log)
    hc = hunter.connect()
    results = {}
    try:
        for cat in cats:
            if should_stop():
                break
            sid = ensure_source(hc, cat)
            job = hc.execute("SELECT id FROM hunter_jobs WHERE source_id=? AND status='queued' ORDER BY id LIMIT 1", (sid,)).fetchone()
            jid = job["id"] if job else hc.execute("INSERT INTO hunter_jobs(source_id,kind,requested,status,params) VALUES(?,?,?,?,?)",
                                                   (sid, "daily", hunter.now(), "running", "{}")).lastrowid
            hc.execute("UPDATE hunter_jobs SET status='running', started=? WHERE id=?", (hunter.now(), jid))
            hc.commit()
            st = {}
            try:
                recs = fetch_records(cat, int(conf.get("days_back", 30)), net, log, should_stop, st)
                src = dict(hc.execute("SELECT * FROM hunter_sources WHERE id=?", (sid,)).fetchone())
                ests = [(r["case_number"], r.pop("_est_value", None)) for r in recs]
                stats = hunter.ingest(hc, src, recs, jid, log)
                _est_values(hc, cat, ests)
                cov = "operational" if stats["accepted"] or st.get("cases") else "connected_no_records"
                hc.execute("""UPDATE hunter_jobs SET status='done', finished=?, fetched=?, accepted=?, new_events=?, duplicates=?, rejected=?, errors=? WHERE id=?""",
                           (hunter.now(), stats["fetched"], stats["accepted"], stats["new_events"], stats["duplicates"], stats["rejected"],
                            hunter._j(stats["errors"][:50]), jid))
                hc.execute("UPDATE hunter_sources SET last_success=?, last_attempt=?, coverage_status=?, last_error=NULL WHERE id=?",
                           (hunter.now(), hunter.now(), cov, sid))
                results[cat] = {**st, "new": stats["new_events"]}
            except Exception as e:  # noqa: BLE001  one category failing never stops the others
                err = f"{e.__class__.__name__}: {str(e)[:300]}"
                hc.execute("UPDATE hunter_jobs SET status='failed', finished=?, errors=? WHERE id=?", (hunter.now(), hunter._j([{"reason": err}]), jid))
                hc.execute("UPDATE hunter_sources SET last_attempt=?, last_error=? WHERE id=?", (hunter.now(), err, sid))
                results[cat] = {"error": err}
            hc.commit()
    finally:
        db.kv_set(mcon, "clerk_last", json.dumps({"at": db.now(), "lookups": net.lookups, "results": results}))
        mcon.close()
        hc.close()
    parts = [f"{CAT_SHORT[c]} {r.get('new', 0)} new" + (" (more tomorrow)" if r.get("deferred") else "") if "error" not in r else f"{CAT_SHORT[c]} failed"
             for c, r in results.items()]
    log(f"[{datetime.now():%H:%M}] Pinellas Clerk records: " + ", ".join(parts) + f" ({net.lookups} appraiser lookups).")
    return results


CAT_SHORT = {"preforeclosure": "pre-foreclosure", "probate": "probate", "eviction": "evictions"}


def _est_values(hc, cat, ests):
    from . import hunter
    for case, v in ests:
        if not v:
            continue
        for r in hc.execute("SELECT DISTINCT property_id FROM lead_events WHERE category=? AND case_ref=? AND jurisdiction='Pinellas FL'", (cat, case)):
            hc.execute("UPDATE properties SET est_value=COALESCE(est_value, ?), est_value_source=COALESCE(est_value_source, ?) WHERE id=?",
                       (v, "county appraiser sales comparison", r["property_id"]))
            hunter.score_property(hc, r["property_id"])
    hc.commit()
