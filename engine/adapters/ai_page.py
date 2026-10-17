"""Any public county page: Claude reads it with your instructions.
counties.yaml options: url (required), instructions, next_selector (CSS for a 'next page' link), wait_selector."""
from .base import browser, check_blocked, pause
from .. import ai


def fetch(county, cfg, log=print):
    instr = county.get("instructions") or "List of distressed-property records (foreclosure, lis pendens, tax deed, probate, code violations)."
    max_pages = int(county.get("max_pages") or cfg.get("scrape", {}).get("max_pages", 15))
    out = []
    with browser(cfg) as page:
        page.goto(county["url"], wait_until="networkidle", timeout=60000)
        for n in range(max_pages):
            if county.get("wait_selector"):
                page.wait_for_selector(county["wait_selector"], timeout=15000)
            check_blocked(page)
            text = page.inner_text("body")
            recs = ai.extract_records(text, instr, cfg["anthropic_model"])
            for r in recs:
                r.update(county=county["name"], state=county.get("state", ""), url=page.url, raw=dict(r))
                r.setdefault("source_type", county.get("source_type", "other"))
            out.extend(recs)
            log(f"  page {n + 1}: {len(recs)} records")
            sel = county.get("next_selector")
            if not sel or not page.query_selector(sel):
                break
            page.click(sel)
            page.wait_for_load_state("networkidle")
            pause(cfg)
    return out
