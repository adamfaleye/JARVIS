"""Owner lookup from the county property appraiser's public records.

The auction sites (RealAuction) list a property and the amount owed, but no owner. Skip tracing (AvaData) needs the owner's
name and mailing address to find a phone number, so before a lead is exported this fills in, from the appraiser:
owner name(s), mailing address, property use, year built, living area, the appraiser's just/market value and its own
sales-comparison estimate. It also flags what tends to signal a motivated owner: an estate, several co-owners
(often heirs), an owner who mails somewhere else, or an out-of-state owner.

Supported: Pinellas County, FL (pcpao.gov public property search). Other counties return "unsupported" and are
left alone. Requests are paced and capped per run; nothing here contacts an owner or costs money.
"""
import html as _html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date

from . import db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS-Engine/1.0 (owner lookup; contact: county public records)"
PINELLAS = "https://www.pcpao.gov"


# ------------------------------------------------------------------ parsing
def _text(fragment):
    s = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    return "\n".join(re.sub(r"[ \t\r]+", " ", ln).strip() for ln in s.split("\n") if ln.strip())


def _by_id(page, el_id):
    m = re.search(r'id="%s"[^>]*>(.*?)</(?:span|label|div|h2)>' % re.escape(el_id), page, re.S | re.I)
    return _text(m.group(1)) if m else ""


def _money(s):
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_pinellas(page):
    """Fields from a pcpao.gov property-details page."""
    names = "\n".join([_by_id(page, "first_second_owner"), _by_id(page, "third_owner"), _by_id(page, "other_owners")])
    owners = []
    for o in names.split("\n"):
        if o.strip() and o.strip() not in owners:
            owners.append(o.strip())
    use = _by_id(page, "property_use")
    m = re.match(r"(\d{4})\s+(.*)", use)
    site = _by_id(page, "site_address").split("\n")
    mail = _by_id(page, "mailling_add").split("\n")  # (sic) the site's own id
    yb = _by_id(page, "Yrb")
    sf = _by_id(page, "tls")
    just = None
    jm = re.search(r"Just/Market Value.*?<tbody>\s*<tr>\s*<td>\s*(\d{4})\s*</td>\s*<td>\s*([^<]+)</td>", page, re.S | re.I)
    if jm:
        just = _money(jm.group(2))
    sc = _money(_by_id(page, "sc"))
    deed = re.search(r"getCocDeedBkPg\?bk=(\d+)&(?:amp;)?pg=(\d+)", page)
    return {
        "owners": owners,
        "use_code": m.group(1) if m else "", "use": (m.group(2) if m else use).strip(),
        "site_street": site[0] if site else "", "site_city_line": site[1] if len(site) > 1 else "",
        "mailing_address": ", ".join(x for x in mail if x),
        "year_built": int(yb) if yb.isdigit() else None,
        "living_sqft": _money(sf),
        "just_value": just, "just_value_year": int(jm.group(1)) if jm else None,
        "sales_comparison": sc,
        "deed_book_page": f"{deed.group(1)}/{deed.group(2)}" if deed else "",
    }


def _state_of(mailing):
    m = re.search(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$", (mailing or "").upper())
    return m.group(1) if m else ""


def signals(info, site_street=""):
    """What in the record points to a motivated owner. Facts only, each with its reason."""
    out = []
    owners = info.get("owners") or []
    joined = " / ".join(owners).upper()
    if re.search(r"\bEST\b|\bESTATE\b|\bDECEASED\b|\bHEIRS?\b", joined):
        out.append({"key": "estate", "why": "Owner of record is an estate (" + joined.title() + ")"})
    names = [set(re.findall(r"[A-Z]+", o.upper())) for o in owners if o.strip()]
    shared = set.intersection(*names) if names else set()
    if len(owners) >= 3:
        out.append({"key": "co_owners", "why": f"{len(owners)} owners on title" + (" sharing a family name (possibly heirs)" if shared else "")})
    if re.search(r"\bTRUST\b|\bTRE\b|\bTR\b", joined):
        out.append({"key": "trust", "why": "Held in a trust"})
    mail = info.get("mailing_address") or ""
    street = site_street or info.get("site_street") or ""
    same = lambda a, b: sorted(t for t in _norm_site(a).split() if t != "unit") == sorted(t for t in _norm_site(b).split() if t != "unit")  # noqa: E731
    if mail and street and not same(mail, street):
        out.append({"key": "absentee", "why": "Owner's mailing address is elsewhere: " + mail})
    st = _state_of(mail)
    if st and st != "FL":
        out.append({"key": "out_of_state", "why": f"Owner mails out of state ({st})"})
    return out


# ------------------------------------------------------------------ fetching
def _get(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _norm_site(s):
    """Street and unit for matching: '100 SAMPLE DR S # 202' and '100 Sample Dr S #202' compare equal."""
    s = str(s or "").upper().split(",")[0].replace("#", " UNIT ")
    s = re.sub(r"\b(APT|STE|SUITE|UNIT|NO)\b\.?", " UNIT ", s)
    return db.norm_addr(re.sub(r"\s+", " ", s))


def pinellas_lookup(street, fetch=_get):
    """Search the appraiser by street address and read the matching parcel. Returns (info, status)."""
    q = re.sub(r"#\s*", "", str(street or "")).strip()
    if not re.match(r"^\d", q):
        return None, "no street number"
    raw = fetch(PINELLAS + "/dal/quicksearch/homeSearch", data=urllib.parse.urlencode({"input": q, "option": "address"}).encode(),
                headers={"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    try:
        hits = json.loads(raw) or []
    except ValueError:
        return None, "search error"
    want = _norm_site(street)
    exact = [h for h in hits if _norm_site(h.get("sa")) == want]
    if not exact:
        return None, "not found" if not hits else f"no exact match ({len(hits)} similar)"
    if len({h.get("s") for h in exact}) > 1:
        return None, "several parcels match"
    page = fetch(PINELLAS + "/property-details?s=" + urllib.parse.quote(exact[0]["s"]))
    info = parse_pinellas(page)
    if not info["owners"]:
        return None, "parcel page had no owner"
    info["parcel_number"] = exact[0].get("pn") or ""
    info["source"] = "Pinellas County Property Appraiser (pcpao.gov)"
    info["source_url"] = PINELLAS + "/property-details?s=" + exact[0]["s"]
    return info, "ok"


LOOKUPS = {("pinellas", "fl"): pinellas_lookup}


def supported(county, state):
    return (str(county or "").strip().lower(), str(state or "").strip().lower()) in LOOKUPS


# ------------------------------------------------------------------ storing
def _ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    for c in ("owner_info", "owner_checked"):
        if c not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {c} TEXT")
    con.commit()


def apply(con, lead, info):
    """Save what the appraiser says onto the county lead and into The Hunter (owner, mailing address, property facts), then rescore."""
    from . import hunter
    owners = info["owners"]
    sig = signals(info, lead["address"])
    con.execute("UPDATE leads SET owner=?, owner_info=?, owner_checked=? WHERE id=?",
                (" / ".join(owners), json.dumps({**info, "signals": sig}), db.now(), lead["id"]))
    con.commit()  # one writer at a time: finish this before The Hunter's connection writes
    hc = hunter.connect()
    street, city, _st, zip_ = hunter.split_street(lead["address"])
    sk = db.norm_addr(street)
    rows = hc.execute("SELECT id, addr_key FROM properties WHERE addr_key LIKE ? AND lower(county)=lower(?)", (sk + "|%", lead["county"] or "")).fetchall() if sk else []
    k = hunter.addr_key(street, lead["zip"] or zip_, lead["city"] or city)
    exact = [r["id"] for r in rows if r["addr_key"] == k]
    pids = exact or ([rows[0]["id"]] if len(rows) == 1 else [])  # same street in the same county; never guess between two
    for pid in pids:
        ev = hc.execute("SELECT id FROM lead_events WHERE property_id=? ORDER BY event_date DESC LIMIT 1", (pid,)).fetchone()
        for name in owners:
            oid = hunter.upsert_owner(hc, {"owner": name, "mailing_address": info.get("mailing_address")}, pid, ev["id"] if ev else None)
            if oid:
                hc.execute("UPDATE owner_property SET confidence='county appraiser' WHERE owner_id=? AND property_id=?", (oid, pid))
        hc.execute("""UPDATE properties SET property_type=COALESCE(NULLIF(property_type,''), ?), sqft=COALESCE(sqft, ?), year_built=COALESCE(year_built, ?),
                      est_value=COALESCE(est_value, ?), est_value_source=COALESCE(est_value_source, ?), updated=? WHERE id=?""",
                   (info.get("use"), info.get("living_sqft"), info.get("year_built"), info.get("sales_comparison"),
                    "county appraiser sales comparison" if info.get("sales_comparison") else None, db.now(), pid))
        hunter.score_property(hc, pid)
    hc.commit()
    hc.close()
    return {"owners": owners, "signals": sig, "hunter_properties": len(pids)}


def enrich(con, limit=60, pause_s=2.0, fetch=_get, log=print):
    """Look up owners for county leads still waiting for review whose sale hasn't happened yet, soonest sale first.
    Each lead is tried once (owner_checked); a lead that wasn't found isn't retried."""
    _ensure_columns(con)
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM leads WHERE status IN ('new','exported') AND owner_checked IS NULL AND COALESCE(owner,'')='' AND address IS NOT NULL AND address!='' AND "
        + db.UPCOMING + " ORDER BY CASE WHEN event_date IS NULL OR event_date='' THEN 1 ELSE 0 END, event_date LIMIT ?", (limit * 3,))]
    rows = [r for r in rows if supported(r["county"], r["state"])][:limit]
    stats = {"checked": 0, "found": 0, "not_found": 0, "errors": 0, "estates": 0}
    for i, r in enumerate(rows):
        if i:
            time.sleep(pause_s)  # be polite to the county site
        stats["checked"] += 1
        try:
            info, status = LOOKUPS[(r["county"].strip().lower(), r["state"].strip().lower())](r["address"], fetch=fetch)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            if stats["errors"] >= 3:
                log(f"Owner lookup stopped after repeated errors: {e}")
                break
            continue
        if not info:
            stats["not_found"] += 1
            con.execute("UPDATE leads SET owner_checked=?, owner_info=? WHERE id=?", (db.now(), json.dumps({"status": status}), r["id"]))
            con.commit()
            continue
        res = apply(con, r, info)
        stats["found"] += 1
        stats["estates"] += any(s["key"] == "estate" for s in res["signals"])
    if stats["checked"]:
        log(f"Owner lookup: {stats['found']} of {stats['checked']} county leads now have the owner on file"
            + (f" ({stats['estates']} estate{'s' if stats['estates'] != 1 else ''})" if stats["estates"] else "") + ".")
    db.kv_set(con, "owner_lookup_last", json.dumps({**stats, "at": db.now(), "day": date.today().isoformat()}))
    return stats
