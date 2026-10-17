# JARVIS Engine — notes for Claude Code

Owner: Ademola (Mola) Faleye, real estate wholesaler. Windows machine. He wants things done, not explained.

## What this is
Local automation for his JARVIS CRM (a claude.ai artifact). Python 3.11+, Playwright (Chromium), Anthropic SDK, Twilio, SQLite, MCP (FastMCP).
- `run.py`: CLI (scrape, export, add-county, counties, sms enroll/send/poll/status, feed, loop)
- `engine/adapters/*`: county source adapters, each `fetch(county, cfg, log) -> list[dict]` with record fields:
  county, state, source_type, case_number, parcel_id, address, city, zip, owner, judgment_amount, assessed_value, event_date (YYYY-MM-DD), url, raw
- `engine/db.py`: SQLite schema and dedupe (case number per county/source, else normalized address)
- `mcp_server.py`: tools for Claude Desktop, incl. `crm_pull`/`crm_push` used by the JARVIS app's PC link
- `engine/crm.py`: local mirror of the app's CRM, data model v2 (stages New Lead → Closed, archive-with-reason, one `fu_<dealId>` follow-up task per deal, audit entries with source "desktop"). Table `crm_items`, newest `_ts` wins. Shapes and rules must match the app; writes go through the validated ops (amounts/dates must come from Mola's own words).
- `voice/jarvis_voice.py`: hands-free assistant (openWakeWord "hey_jarvis" → faster-whisper → Claude tool loop → edge-tts/pyttsx3); `python run.py voice`

## Setup task
Create a venv, `pip install -r requirements.txt`, `playwright install chromium`, copy `.env.example` to `.env` (ask him for ANTHROPIC_API_KEY), then run `python run.py scrape --county Pinellas` and report found/new counts.

## Building a custom county adapter
1. Copy `engine/adapters/_template.py` to `engine/adapters/<county>_<state>.py`.
2. Inspect the site with Playwright in headed mode (`scrape.headless: false`), fill the search form, and parse the result rows.
3. Add `adapter: custom:<county>_<state>` in `counties.yaml`, run it, and show sample records.

Rules:
- Pace requests.
- Never bypass CAPTCHAs or log-ins; raise `Blocked` instead.
- Never fabricate fields.

## Texting
Keep `require_consent: true` unless Mola explicitly changes it after being told the TCPA risk. Never remove STOP/DNC handling or quiet hours.
