"""Template for a county with a search form (clerk official records, lis pendens, probate).
Copy to engine/adapters/<county>_<state>.py and set adapter: custom:<county>_<state> in counties.yaml.
Easiest path: open this folder in Claude Code and say
  "Build a custom adapter for <County>, <ST> lis pendens filings from <url> using _template.py"."""
from datetime import date, timedelta
from .base import browser, check_blocked, pause


def fetch(county, cfg, log=print):
    out = []
    since = date.today() - timedelta(days=int(county.get("lookback_days", 7)))
    with browser(cfg) as page:
        page.goto(county["url"], wait_until="networkidle")
        check_blocked(page)
        # 1. Fill the search form, e.g.:
        # page.select_option("#DocType", "LIS PENDENS")
        # page.fill("#StartDate", since.strftime("%m/%d/%Y"))
        # page.click("#Search")
        # 2. Read result rows:
        # for row in page.query_selector_all("table#results tr")[1:]:
        #     cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
        #     out.append({"county": county["name"], "state": county["state"], "source_type": "lis_pendens",
        #                 "case_number": cells[0], "owner": cells[2], "event_date": cells[1], "url": page.url})
        pause(cfg)
    return out
