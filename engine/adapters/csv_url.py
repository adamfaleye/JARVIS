"""County publishes a CSV. counties.yaml: url, and optional columns map {field: "Header Name"}."""
import csv
import io
import urllib.request


def fetch(county, cfg, log=print):
    req = urllib.request.Request(county["url"], headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    cols = county.get("columns") or {}
    guess = {
        "address": ["Property Address", "Site Address", "Address", "Situs Address"],
        "city": ["City", "Property City", "Site City"], "zip": ["Zip", "Zip Code", "Property Zip"],
        "owner": ["Owner", "Owner Name", "Defendant"], "case_number": ["Case Number", "Case #", "Case"],
        "parcel_id": ["Parcel ID", "Parcel", "Folio"], "event_date": ["Date", "Filing Date", "Sale Date"],
    }
    out = []
    for r in rows:
        rec = {"county": county["name"], "state": county.get("state", ""), "url": county["url"],
               "source_type": county.get("source_type", "other"), "raw": r}
        for f, opts in guess.items():
            h = cols.get(f)
            vals = [r.get(h)] if h else [r.get(o) for o in opts]
            rec[f] = next((v.strip() for v in vals if v), "")
        out.append(rec)
    log(f"  {len(out)} rows")
    return out
