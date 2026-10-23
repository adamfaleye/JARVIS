# JARVIS

**A voice-controlled AI agent that runs a real estate acquisitions business.** Say "Hey Jarvis" from across the room and it reads your call queue, logs calls, moves deals through the pipeline, matches buyers, and runs the numbers on a property. Behind the voice, it scrapes county records for motivated sellers every morning, scores them, and runs compliant SMS follow-up through Twilio, with every outbound message approved by a human.

You can also walk through it. **MolaBucks Capital HQ** is a 3D office where JARVIS acts as CEO over six AI agents. Each agent works a real task queue, and what you see in the office reflects their actual state. It runs in a browser or on a Meta Quest 2 through WebXR.

I built it to run my own wholesaling business, MolaBucks Capital, and it's in daily use.

`Python` · `Claude API (tool use)` · `MCP` · `Playwright` · `Twilio` · `SQLite` · `Three.js` · `WebXR` · `faster-whisper` · `openWakeWord` · `ElevenLabs`

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

### MolaBucks Capital HQ: the agent team in 3D (`engine/hq.py`, `web/jarvis.html`)
- **Six AI employees under a JARVIS "CEO":**
  - Hunter: acquisitions
  - Closer: dispositions
  - Dispositions: buyer matching
  - Follow-up
  - Front Desk: communications intake
  - Finance
- **A durable task queue in SQLite.** Each task has an owner, status, attempts and an event log. Workers lease a task so no two processes run the same one. Failures retry with backoff and are marked **blocked** with a reason instead of being silently skipped.
- **A task only counts as done when its result checks out.** JARVIS turns a monthly revenue goal into a plan by working backward from collected fees through closings, contracts, offers and conversations, then assigns the day's work.
- **Approval levels:**
  - Internal work runs on its own.
  - Drafts wait for review.
  - Outside commitments, such as launching a campaign or spending money, go to an approval station. Each approval covers only the exact parameters you reviewed, and no agent can approve its own work.
- **A walkable 3D office built on Three.js.** It shows the system's real state:
  - Agents type only while their task is running.
  - An agent waiting on approval raises its hand.
  - Failed agents slump under a red marker.
  - The HUD, minimap and objectives are built from live data.
- **Quality and fallback:** graphics presets adjust automatically to the frame rate. Devices without 3D support get a plain Operations list instead.

### VR on Meta Quest 2 (`engine/web.py`, `run.py web`)
- `run.py web` serves HQ over HTTPS on the local network, using the same `crm_pull` / `crm_push` sync as the desktop link. Devices pair with a single-use 6-digit code.
- **WebXR mode:**
  - Smooth movement and snap turn.
  - Collision.
  - In-world menus and panels.
  - Approvals with confirmation.
- **VR-specific rendering profile:** merged geometry and no post-processing. This cut lobby draw calls from 528 to 135.
- **Verified in Meta's IWER Quest 2 emulator; not yet tested on a physical headset.** The setup guide and test checklist are in [`docs/VR.md`](docs/VR.md).

### Integrations (`mcp_server.py`)
- An **MCP server** exposes the engine to Claude Desktop ("add Lee County", "how many new leads today?") and syncs the JARVIS web app with the local CRM (newest-timestamp-wins merge).

## Engineering notes
- **160 automated checks** across four suites:
  - `tests/test_hunter.py`: dedupe, scoring, skip-trace import.
  - `tests/test_outreach.py`: DNC enforcement, drip timing, sender mapping.
  - `tests/test_hq.py`: task leasing, retries, blocked dependencies, approval binding.
  - `tests/test_web.py`: pairing and the HTTPS sync server.
- Scrapers pace their requests, never bypass CAPTCHAs or logins (they raise `Blocked` instead), and never fabricate fields.
- All lead and contact data stays local in SQLite. Nothing personal is in this repo.

## Run it

```powershell
pip install -r requirements.txt
playwright install chromium
copy .env.example .env          # add your Anthropic key; Twilio keys optional
python run.py scrape            # pull every configured county
python run.py voice             # needs requirements-voice.txt
python run.py web               # serve HQ to a Quest / other devices on your network
python -m tests.test_hunter     # run the tests (also test_outreach, test_hq, test_web)
```

Full Windows setup, county configuration, and texting compliance notes are in [`docs/SETUP.md`](docs/SETUP.md). How HQ works is in [`docs/HQ.md`](docs/HQ.md), VR setup is in [`docs/VR.md`](docs/VR.md), and the system design and roadmap are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The Hunter's scoring model is in [`docs/HUNTER.md`](docs/HUNTER.md), and the outreach rules are in [`docs/OUTREACH.md`](docs/OUTREACH.md).

---

Built by **Ademola Faleye** · [LinkedIn](https://linkedin.com/in/ademola-faleye-5b19ab365)
