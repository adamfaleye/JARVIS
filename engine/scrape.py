"""Run county adapters, store new leads, export files for skip tracing and JARVIS import."""
import csv
import re
import urllib.request
from . import db, config
from .adapters import get as get_adapter
from .adapters.base import Blocked


def run(cfg, only=None, log=print):
    con = db.connect()
    summary = []
    for c in cfg["counties"]:
        if not c.get("enabled", True):
            continue
        label = f"{c['name']} {c.get('state','')} ({c.get('sale_type') or c.get('adapter')})"
        if only and only.lower() not in c["name"].lower():
            continue
        from . import hunter
        hcon = hunter.connect()
        hunter.sync_registry(hcon, cfg)
        if hunter.is_paused(hcon, c):
            log(f"Skipping {label}: paused in The Hunter.")
            continue
        log(f"Scraping {label}…")
        started, found, new, err = db.now(), 0, 0, None
        try:
            recs = get_adapter(c.get("adapter")).fetch(c, cfg, log)
            found = len(recs)
            for r in recs:
                if db.insert_lead(con, r):
                    new += 1
            con.commit()
            try:
                hs = hunter.record_scrape(hcon, c, recs, log)
                if hs:
                    log(f"  Hunter: {hs['new_events']} new events, {hs['duplicates']} already known, {hs['rejected']} rejected")
            except Exception as he:
                log(f"  Hunter ingest error: {he}")
        except Blocked as e:
            err = str(e)
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
        con.execute("INSERT INTO runs(kind,target,started,finished,found,new,error) VALUES('scrape',?,?,?,?,?,?)",
                    (label, started, db.now(), found, new, err))
        con.commit()
        log(f"  → {found} found, {new} new" + (f" | ERROR {err}" if err else ""))
        summary.append({"source": label, "found": found, "new": new, "error": err})
    return summary


def export(log=print):
    """Writes out/jarvis_leads_import.csv (upload to JARVIS › Pipeline › Import › Seller leads)
    and out/skiptrace.csv (upload to your skip-trace provider). Marks leads exported."""
    con = db.connect()
    rows = con.execute("SELECT * FROM leads WHERE status IN ('new','exported') AND exported_at IS NULL ORDER BY event_date").fetchall()
    if not rows:
        log("No new leads to export.")
        return 0
    out = config.path("out")
    out.mkdir(exist_ok=True)
    stamp = db.now()[:10]
    jp, sp = out / f"jarvis_leads_import_{stamp}.csv", out / f"skiptrace_{stamp}.csv"
    with open(jp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Property Address", "City", "State", "Zip", "County", "Owner Name", "Notes"])
        for r in rows:
            note = "; ".join(x for x in [
                (r["source_type"] or "").replace("_", " "),
                f"Case {r['case_number']}" if r["case_number"] else "",
                f"Sale/filing {r['event_date']}" if r["event_date"] else "",
                f"Judgment ${r['judgment_amount']:,.0f}" if r["judgment_amount"] else "",
                f"Assessed ${r['assessed_value']:,.0f}" if r["assessed_value"] else "",
                f"Parcel {r['parcel_id']}" if r["parcel_id"] else ""] if x)
            w.writerow([r["address"], r["city"], r["state"], r["zip"], r["county"], r["owner"] or "", note])
    with open(sp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Owner Name", "Property Address", "City", "State", "Zip"])
        for r in rows:
            w.writerow([r["owner"] or "", r["address"], r["city"], r["state"], r["zip"]])
    stamp_now = db.now()
    con.executemany("UPDATE leads SET exported_at=? WHERE id=?", [(stamp_now, r["id"]) for r in rows])
    con.commit()
    log(f"Exported {len(rows)} leads:\n  {jp}\n  {sp}")
    return len(rows)


PROBES = [
    ("foreclosure", "https://{slug}.realforeclose.com"),
    ("taxdeed", "https://{slug}.realtaxdeed.com"),
]
OH_PROBE = ("sheriff", "https://{slug}.sheriffsaleauction.ohio.gov")


def _reachable(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(20000).decode("utf-8", "ignore").lower()
            return r.status == 200 and ("auction" in body or "zaction" in body)
    except Exception:
        return False


def add_county(name, state, url=None, adapter=None, instructions=None, log=print):
    """Adds a county source. With no URL, auto-detects RealAuction foreclosure / tax-deed / sheriff sale sites."""
    name = re.sub(r"\s+county$", "", name.strip(), flags=re.I)
    state = state.upper()
    counties = config.load_counties()
    added = []
    if url:
        entry = {"name": name, "state": state, "adapter": adapter or ("realauction" if "real" in url or "sheriffsaleauction" in url else "ai_page"),
                 "url": url, "enabled": True}
        if entry["adapter"] == "ai_page":
            entry["instructions"] = instructions or "Distressed-property records (foreclosure, lis pendens, tax deed, probate); one per property."
        added.append(entry)
    else:
        slug = re.sub(r"[^a-z]", "", name.lower())
        probes = PROBES + ([OH_PROBE] if state == "OH" else [])
        for sale_type, pat in probes:
            u = pat.format(slug=slug)
            log(f"Checking {u} …")
            if _reachable(u):
                added.append({"name": name, "state": state, "adapter": "realauction", "url": u,
                              "sale_type": sale_type, "enabled": True})
    existing = {(c["name"].lower(), c.get("url", "").lower()) for c in counties}
    added = [a for a in added if (a["name"].lower(), a["url"].lower()) not in existing]
    if added:
        config.save_counties(counties + added)
    return added
