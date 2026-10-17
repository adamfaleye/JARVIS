# JARVIS

**A voice-controlled AI agent that runs a real estate acquisitions business.** Say "Hey Jarvis" from across the room and it reads your call queue, logs calls, moves deals through the pipeline, matches buyers, and runs the numbers on a property. Behind the voice, it scrapes county records for motivated sellers every morning, scores them, and runs compliant SMS follow-up through Twilio, with every outbound message approved by a human.

I built it to run my own wholesaling business, MolaBucks Capital, and it's in daily use.

`Python` · `Claude API (tool use)` · `MCP` · `Playwright` · `Twilio` · `SQLite` · `faster-whisper` · `openWakeWord` · `ElevenLabs`

---

## How it works

```
                  "Hey Jarvis, who do I call first?"
                                 │
        ┌────────────────────────▼────────────────────────┐
        │  VOICE   openWakeWord → faster-whisper (STT)    │
        │          → Claude tool loop → ElevenLabs (TTS)  │
        └────────────────────────┬────────────────────────┘
                                 │ 18 typed tools
   ┌──────────────┬──────────────┼──────────────┬──────────────┐
   ▼              ▼              ▼              ▼              ▼
THE HUNTER     CRM MIRROR     THE CLOSER     DEAL FEED      MCP SERVER
county         pipeline,      SMS drips,     WhatsApp       Claude Desktop
scrapers →     tasks, buyers, AI-drafted     exports →      + the JARVIS
scoring →      audit log      replies,       buyer          web app sync
skip-trace                    DNC / STOP     matching
   └──────────────┴──────────────┴──────────────┴──────────────┘
                                 │
                          SQLite (local)
```

### The Hunter: motivated-seller sourcing (`engine/hunter.py`, `engine/adapters/`)
- **Pluggable county adapters.** RealAuction sites (foreclosure, tax-deed, Ohio sheriff sales), public CSV feeds, and an `ai_page` adapter where Claude reads an arbitrary county records page from plain-English instructions. Adding a county is one command: `python run.py add-county "Pasco" --state FL`.
- **16 distress categories** (probate, pre-foreclosure, tax delinquent, bankruptcy, code violation, and more), deduplicated by case number or normalized address and **scored into A/B/C tiers** with a stated reason for every score.
- **Skip-trace round trip.** Exports trace files, re-imports results, and attaches phones to the right property, with manual review for ambiguous matches.

### Voice agent (`voice/jarvis_voice.py`)
- Hands-free loop: wake word → local Whisper transcription → **Claude tool use over 18 CRM tools** → spoken reply, with follow-up listening so you don't repeat the wake word.
- **Grounding guards.** Every dollar amount and date the agent writes must come with the user's exact words (`amounts_said`, `dates_said`), validated in `engine/crm.py` before anything is saved. The model can't invent an offer price or a follow-up date.
- Read/write separation: reads run freely, writes go through validated operations and leave an audit entry.
- TTS fallback chain: ElevenLabs → Edge neural voice → Windows SAPI.

### The Closer: compliant SMS outreach (`engine/outreach.py`, `engine/agent.py`, `engine/sms.py`)
- Multi-step drip campaigns that **stop on reply**, respect quiet hours and a daily cap, and honor STOP and a Do-Not-Contact list checked again at send time, not just at enrollment.
- Sticky sender mapping, so a lead always hears from the same number.
- When a lead replies, **Claude drafts the response; a human taps Send.** Nothing goes out unreviewed.
- Click-to-call bridging through Twilio: rings your cell, then connects the lead from the number they know.

### Integrations (`mcp_server.py`)
- An **MCP server** exposes the engine to Claude Desktop ("add Lee County", "how many new leads today?") and syncs the JARVIS web app with the local CRM (newest-timestamp-wins merge).

## Engineering notes
- **88 automated checks** across `tests/test_hunter.py` and `tests/test_outreach.py`, covering dedupe, scoring, skip-trace import, DNC enforcement, drip timing, sender mapping, and the read/write boundary.
- Scrapers pace their requests, never bypass CAPTCHAs or logins (they raise `Blocked` instead), and never fabricate fields.
- All lead and contact data stays local in SQLite. Nothing personal is in this repo.

## Run it

```powershell
pip install -r requirements.txt
playwright install chromium
copy .env.example .env          # add your Anthropic key; Twilio keys optional
python run.py scrape            # pull every configured county
python run.py voice             # needs requirements-voice.txt
python -m tests.test_hunter     # run the tests
```

Full Windows setup, county configuration, and texting compliance notes are in [`docs/SETUP.md`](docs/SETUP.md). The Hunter's scoring model is in [`docs/HUNTER.md`](docs/HUNTER.md), and the outreach rules are in [`docs/OUTREACH.md`](docs/OUTREACH.md).

---

Built by **Ademola Faleye** · [LinkedIn](https://linkedin.com/in/ademola-faleye-5b19ab365)
