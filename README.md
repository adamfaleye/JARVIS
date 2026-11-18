# JARVIS

**A Claude multi-agent operating system that runs two real businesses from one engine.** JARVIS started as a voice-controlled AI agent for my real estate acquisitions company, MolaBucks Capital. JARVIS 2.0 now also runs my AI automation agency, **AI Sidekicks**. The two businesses share one engine but keep separate workspaces, records, and audit logs.

Say "Hey Jarvis" from across the room and it reads your call queue, logs calls, moves deals, and runs the numbers on a property. Behind the voice, the engine:

- scrapes county records for motivated sellers,
- finds and researches small-business prospects for the agency,
- drafts outreach for a human to approve,
- runs an Instagram DM assistant,
- keeps a durable task queue moving for 18 AI agents.

I built it to run my own businesses, and I use it every day.

`Python` · `Claude API (tool use)` · `MCP` · `Playwright` · `SQLite` · `Twilio` · `Gmail` · `Instagram Graph API` · `Three.js` · `WebXR` · `faster-whisper` · `openWakeWord` · `ElevenLabs`

---

## How it works

```
                 "Hey Jarvis, what needs me today?"
                                │
       ┌────────────────────────▼────────────────────────┐
       │  VOICE   openWakeWord → faster-whisper (STT)    │
       │          → Claude tool loop → ElevenLabs (TTS)  │
       └────────────────────────┬────────────────────────┘
                                │ 29 typed tools
 ┌──────────────────────────────▼───────────────────────────────┐
 │  HQ ENGINE (engine/hq.py)  durable task queue · leases ·      │
 │  retries · hash-bound approvals · per-business audit log      │
 │  18 agents (org.py) · daily AI spend cap (agents_ai.py)       │
 └──────┬──────────────────────┬─────────────────────┬──────────┘
        ▼                      ▼                     ▼
  MOLABUCKS CAPITAL       AI SIDEKICKS           INTEGRATIONS
  Hunter (county          SDR research,          MCP server (Claude
  records → scoring),     Gmail outreach,        Desktop + app sync),
  Closer (SMS),           Instagram DM           Telegram line,
  deal feed, CRM          assistant, client CRM  HTTPS/VR web server
        └──────────────────────┴─────────────────────┘
                               │
                        SQLite (local only)
```

### Two businesses, one engine (JARVIS 2.0)
* **Kept separate by business.** Every task and record carries its business, the assistant can only use the active business's operations, and every audit entry goes to that business's own log. The executive overview reads both businesses and writes to neither. See `docs/JARVIS2.md`.
* **18 AI agents** in four groups: Executive Office, MolaBucks Capital, and AI Sidekicks Sales, Delivery and Client Success (`engine/org.py`). An agent shows as live only when real code runs its work.
* **Controlled autonomy, enforced in the backend.**
  + Internal work runs on its own.
  + Drafts wait for review.
  + Outside commitments wait for an approval. Each approval is bound to a hash of the exact parameters you reviewed, so editing a detail afterward voids it, and no agent can approve its own work.
* **Cost control.** Every Claude call is priced before it runs and checked against a daily AI spend cap. Each agent also has its own budget inside that cap, so one agent can't use up the whole day (`engine/agents_ai.py`).

### AI Sidekicks
* **AI SDR.** It finds small and midsize businesses online, scores fit, and drafts tailored intro emails from verified facts. Every fact in its CRM carries a source URL and a confidence grade (`engine/dcrm.py`, `engine/hq.py`).
* **Approval-gated Gmail outreach.**
  + Every batch needs a hash-bound approval of the exact emails shown.
  + Sending is paced under daily limits and only in the hours you set.
  + Sends are retry-safe.
  + A do-not-email list is checked at send time (`engine/gmail.py`).
* **Instagram DM assistant.** It answers questions about AI Sidekicks' services and turns interest into discovery-call requests. Nothing is booked until the owner verifies the slot and approves it. It runs in test mode until the pilot is approved (`engine/igassist/`, `docs/AISIDEKICKS.md`).

### MolaBucks Capital
* **The Hunter.** Pluggable county adapters cover RealAuction foreclosure, tax-deed and Ohio sheriff sales, public CSV feeds, a clerk adapter, and an `ai_page` adapter where Claude reads a records page from plain-English instructions. Leads fall into **16 distress categories**, get deduplicated, and are **scored into A/B/C tiers** with a stated reason for each score.
* **The Closer.** SMS drip campaigns stop when a lead replies, respect quiet hours and a daily cap, and check STOP and Do-Not-Contact again at send time. Claude drafts replies, and a human taps Send.

### Voice agent (`voice/jarvis_voice.py`)
* The hands-free loop runs wake word → local Whisper transcription → **Claude tool use over 29 typed tools** → spoken reply.
* **Grounding guards.** Any dollar amount or date the agent writes must match the user's exact words, or the write is rejected.

### HQ: the agent team in 3D (`engine/hq.py`, `web/jarvis.html`)
* **A walkable Three.js office** where each agent works at its own desk on live data.
* Agents type only while their task is running, and an agent raises its hand when it's waiting for an approval.
* **VR on Meta Quest 2.** It runs in WebXR, with HTTPS pairing through a single-use code. It's verified in Meta's IWER emulator. See `docs/VR.md`.

### Integrations
* **MCP server** (`mcp_server.py`). It exposes the engine to Claude Desktop and syncs the JARVIS web app with the local CRM (the newest timestamp wins).

## Engineering notes

* **900+ automated checks in 26 test suites** (`tests/`), covering:
  + the task queue, leases, retries and approval binding,
  + DNC enforcement,
  + scoring and dedupe,
  + CRM provenance,
  + email and Instagram workflows,
  + the voice bridge,
  + the web server.
* Two suites (`test_owner_lookup`, `test_clerk_pinellas`) replay real public-record pages that stay off GitHub, so they skip when those fixtures are missing.
* Scrapers pace their requests, never bypass CAPTCHAs or logins (they raise `Blocked` instead), and never fabricate fields.
* **No secrets or personal data in this repo.**
  + API keys live only in a local `.env`. `.env.example` lists the variable names.
  + OAuth tokens, TLS keys, databases, lead and client data, and logs are all git-ignored.

## Run it

```
pip install -r requirements.txt
playwright install chromium
copy .env.example .env          # add your Anthropic key; other keys optional
python run.py scrape            # pull every configured county
python run.py voice             # needs requirements-voice.txt
python run.py web               # serve HQ to a Quest / other devices on your network
python -m tests.test_hq         # run any suite in tests/
```

* Full Windows setup is in `docs/SETUP.md`.
* The JARVIS 2.0 design is in `docs/JARVIS2.md`.
* HQ is in `docs/HQ.md`, VR in `docs/VR.md`, and the AI Sidekicks assistant in `docs/AISIDEKICKS.md`.
* The Hunter's scoring model is in `docs/HUNTER.md`, and the outreach rules are in `docs/OUTREACH.md`.

---

Built by **Ademola Faleye** · [LinkedIn](https://linkedin.com/in/ademola-faleye-5b19ab365)
