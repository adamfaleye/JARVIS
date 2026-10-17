"""RealAuction platform: <county>.realforeclose.com, <county>.realtaxdeed.com, <county>.sheriffsaleauction.ohio.gov.
Reads the auction preview for each upcoming business day and pulls every listed property."""
import re
from datetime import date, timedelta
from .base import browser, check_blocked, pause, Blocked
from .. import ai

ITEMS_JS = """() => Array.from(document.querySelectorAll('.AUCTION_ITEM')).map(it => {
  const rows = []; let last = null;
  const push = (label, val) => {
    label = (label || '').replace(/:\\s*$/, '').trim(); val = (val || '').trim();
    if (!label && last) { if (val) last.value += ' | ' + val; }
    else if (label || val) { last = {label, value: val}; rows.push(last); }
  };
  const lbls = it.querySelectorAll('.AD_LBL');
  if (lbls.length) {
    lbls.forEach(l => { let d = l.nextElementSibling; while (d && !(d.classList && d.classList.contains('AD_DTA'))) d = d.nextElementSibling; push(l.innerText, d ? d.innerText : ''); });
  } else {
    it.querySelectorAll('tr').forEach(tr => {
      const cells = tr.querySelectorAll('th,td');
      if (cells.length >= 2) push(cells[0].innerText, cells[cells.length - 1].innerText);
      else if (cells.length === 1) push('', cells[0].innerText);
    });
  }
  return {rows, text: it.innerText, html: it.innerHTML.slice(0, 4000)};
})"""

_TEXT_LABELS = r"(Auction Type|Case #|Case Number|Certificate #|Final Judgment Amount|Opening Bid|Parcel ID|Property Address|Assessed Value|Plaintiff Max Bid)"


def _rows_from_text(text):
    """Fallback when the page markup changes: read 'Label: value' pairs from the item's visible text."""
    rows, last = [], None
    lines = [l.strip() for l in re.split(r"[\r\n\t]+", text or "")]
    i = 0
    while i < len(lines):
        m = re.match(_TEXT_LABELS + r"\s*:?\s*(.*)$", lines[i], re.I)
        if m:
            val = m.group(2).strip()
            if not val and i + 1 < len(lines):
                i += 1
                val = lines[i]
            last = {"label": m.group(1), "value": val}
            rows.append(last)
        elif last and last["label"].lower() == "property address" and re.search(r",\s*[A-Z]{2}\b", lines[i]):
            last["value"] += " | " + lines[i]
        i += 1
    return rows


def _pick(rows, *pats):
    for r in rows:
        if any(re.search(p, r["label"], re.I) for p in pats):
            return r["value"]
    return ""


def parse_item(item, county, sale_date, url):
    rows = item.get("rows", [])
    if not _pick(rows, r"property address", r"^address") and not _pick(rows, r"case\s*#", r"case number", r"certificate"):
        rows = _rows_from_text(item.get("text", "")) or rows
    addr_raw = _pick(rows, r"property address", r"^address")
    street, city, zip_ = addr_raw, "", ""
    if "|" in addr_raw:
        street, rest = [x.strip() for x in addr_raw.split("|", 1)]
        m = re.match(r"(.+?),\s*([A-Z]{2})[-\s]*(\d{5})?", rest)
        if m:
            city, zip_ = m.group(1).strip(), (m.group(3) or "")
        else:
            city = rest
    sale_type = county.get("sale_type", "foreclosure")
    return {
        "county": county["name"], "state": county.get("state", ""),
        "source_type": "tax_deed" if sale_type == "taxdeed" else ("sheriff_sale" if sale_type == "sheriff" else "foreclosure_auction"),
        "case_number": _pick(rows, r"case\s*#", r"case number", r"certificate"),
        "parcel_id": _pick(rows, r"parcel"),
        "address": street.strip(), "city": city, "zip": zip_,
        "judgment_amount": _pick(rows, r"judgment", r"opening bid"),
        "assessed_value": _pick(rows, r"assessed", r"market value"),
        "event_date": sale_date.isoformat(), "url": url,
        "raw": {r["label"]: r["value"] for r in rows},
    }


def _debug_dump(county, d, items):
    """Save what the page looked like when no field could be read, so the parser can be fixed."""
    try:
        from ..config import ROOT
        p = ROOT / "data" / "scrape_debug.txt"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"===== {county['name']} {county.get('sale_type','')} {d} =====\n")
            for it in items[:2]:
                f.write("--- text ---\n" + (it.get("text") or "")[:2000] + "\n--- html ---\n" + (it.get("html") or "")[:4000] + "\n")
    except Exception:
        pass


def fetch(county, cfg, log=print):
    base = county["url"].rstrip("/")
    days = int(cfg.get("scrape", {}).get("days_ahead", 30))
    max_pages = int(cfg.get("scrape", {}).get("max_pages", 15))
    out = []
    with browser(cfg) as page:
        d = date.today()
        stop = cfg.get("_should_stop") or (lambda: False)
        for _ in range(days):
            if stop():
                log("  cancelled")
                break
            d += timedelta(days=1)
            if d.weekday() >= 5:
                continue
            url = f"{base}/index.cfm?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={d:%m/%d/%Y}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector(".AUCTION_ITEM", timeout=8000)
                except Exception:
                    check_blocked(page)
                    body = page.inner_text("body")
                    if re.search(r"case\s*#|parcel id", body, re.I):
                        log(f"  {d}: layout changed, letting Claude read the page")
                        for r in ai.extract_records(body, "Upcoming auction list; one record per property.", cfg["anthropic_model"]):
                            r.update(county=county["name"], state=county.get("state", ""), url=url,
                                     event_date=r.get("event_date") or d.isoformat())
                            out.append(r)
                    pause(cfg)
                    continue
                first_seen = None
                for _p in range(max_pages):
                    items = page.evaluate(ITEMS_JS)
                    if not items:
                        break
                    head = items[0]["text"][:80]
                    if head == first_seen:
                        break
                    first_seen = head
                    parsed = [parse_item(i, county, d, url) for i in items]
                    if items and not any(p["address"] or p["case_number"] for p in parsed):
                        _debug_dump(county, d, items)
                    out.extend(parsed)
                    nxt = page.query_selector(".PageRight:visible, [aria-label='Next Page']")
                    if not nxt:
                        break
                    nxt.click()
                    page.wait_for_timeout(2500)
                log(f"  {d}: {len(out)} so far")
            except Blocked:
                raise
            except Exception as e:  # one bad day shouldn't kill the run
                log(f"  {d}: skipped ({e.__class__.__name__}: {str(e)[:120]})")
            pause(cfg)
    return out
