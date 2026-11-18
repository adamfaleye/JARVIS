# JARVIS Engine

This is the part of JARVIS that keeps working when the app is closed. It runs on your Windows PC and serves both businesses: **MolaBucks Capital** (real estate) and **AI Sidekicks** (AI assistants for local businesses). Inside the code and data, AI Sidekicks keeps its original id `demigod`, so older records and tasks still line up. It:

1. **Scrapes county records in any county you add.** It covers foreclosure auctions, tax-deed sales, and Ohio sheriff sales automatically, and any clerk or records page you point it at.
2. **Exports new leads.** Each run writes a JARVIS import file and a skip-trace file.
3. **Texts follow-up sequences through Twilio.** Quiet hours, a daily cap, STOP handling, a DNC list, and stop-on-reply are built in.
4. **Reads your WhatsApp deal feed exports** and matches every property to your buyers.
5. **Talks to Claude Desktop** through an MCP server, so you can say "add Lee County" or "how many new leads today?"
6. **Hands-free voice:** say **"Hey Jarvis"** from anywhere in the room and run your CRM by voice.
7. **PC link:** keeps the JARVIS app and the engine in sync, so whatever you do by voice shows up in the app, and vice versa.

---

## Quick install (recommended)

1. **Unblock the zip:** right-click the zip › Properties › check **Unblock** › OK. Then extract it to `C:\JARVIS`.
2. **Run Setup:** double-click **Setup.bat**. It installs Python if needed and everything JARVIS uses, asks for your Anthropic API key, creates a **JARVIS** icon on your desktop and in the Start menu, and connects JARVIS to Claude Desktop.
3. **Start JARVIS:** double-click the **JARVIS** icon. The engine and voice start minimized in the taskbar. Say "Hey Jarvis."

To stop JARVIS, use **Stop JARVIS** in the Start menu.

## 1. Manual install (if you'd rather do it by hand)

1. Install **Python 3.11+** from python.org. During install, check **"Add Python to PATH."**
2. Unzip this folder somewhere permanent, for example `C:\JARVIS\jarvis-engine`.
3. Open **PowerShell** in that folder and run:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
notepad .env
```

4. In `.env`, paste your **Anthropic API key** (console.anthropic.com). Add the Twilio keys later, when you're ready to text.

**Shortcut:** open this folder in **Claude Code** and say "set this up on my machine and run a test scrape." It reads `CLAUDE.md` and handles the steps above.

## 2. Add counties (any county, any state)

```powershell
python run.py add-county "Pasco" --state FL
python run.py add-county "Lucas" --state OH
python run.py counties
```

With no URL, the engine checks whether the county runs its auctions on RealAuction. It looks for `<county>.realforeclose.com` and `<county>.realtaxdeed.com`, plus the sheriff-sale site for Ohio counties. Many Florida and Ohio counties use these. Whatever it finds gets added automatically.

**Counties that don't use RealAuction**, or **other record types** (lis pendens, probate, code violations, evictions): find the public page that lists them and add it with a URL. Claude reads that page every run.

```powershell
python run.py add-county "Kanawha" --state WV --url "https://<county records page>" --instructions "New lis pendens filings, one per property"
```

**Counties where you must fill out a search form** (most clerk official-records sites): use Claude Code and say
"Build a custom adapter for <County>, <ST> lis pendens from <url> using engine/adapters/_template.py." It writes the adapter and tests it.

You can edit `counties.yaml` directly at any time. Set `enabled: false` to pause a source.

## 3. Run it

```powershell
python run.py scrape            # all counties now, then exports new leads
python run.py scrape --county Pasco
```

New leads land in `out\`:
- **`jarvis_leads_import_<date>.csv`:** In JARVIS, go to Pipeline › Import › Seller leads and upload it. The leads land in your call queue.
- **`skiptrace_<date>.csv`:** Upload to your skip-trace provider. Import the results with phones into JARVIS the same way.

Auction and court lists usually don't include phone numbers, so skip tracing is the step that turns records into callable leads.

## 4. Keep it running automatically

```powershell
python run.py loop
```

This scrapes daily at 6 AM (change `schedule_hour` in `config.yaml`), checks for replies every 5 minutes, sends due texts every 10, and processes the feed inbox every 5. Leave the window open.

To have it start when Windows starts, create a scheduled task. Run this once in PowerShell, with your real folder path:

```powershell
schtasks /Create /TN "JARVIS Engine" /SC ONLOGON /TR "C:\JARVIS\jarvis-engine\.venv\Scripts\python.exe C:\JARVIS\jarvis-engine\run.py loop"
```

## 5. WhatsApp deal feed

1. In WhatsApp, open the deal group › More › **Export chat** › **Without media**. Save the `.txt` into `inbox\feed\`.
2. Put your buyer list in `data\buyers.csv`. It needs columns for name, email/phone, areas, and max price; the exact header names are flexible.
3. Within 5 minutes (or run `python run.py feed`):
   - Every property is extracted.
   - Duplicates are skipped.
   - Matches are written to `out\feed_matches.csv`.
   - A JARVIS import file is written. Upload it under Pipeline › Import › Wholesaler feed deals.

The engine reads exports you drop in the folder rather than logging into WhatsApp. Unofficial WhatsApp bots break WhatsApp's terms and can get your number banned.

## 6. Texting with Twilio (read this before turning it on)

> I'm not a lawyer; check this with one. Under the TCPA, automated marketing texts to people who haven't given **prior express written consent** can carry penalties of **$500–$1,500 per text**. Twilio also requires **A2P 10DLC registration** before business texting from a local number.

That is why `require_consent: true` is the default. Sequences only auto-send to contacts you enroll with `--consent`: sellers who texted or called you first, filled out a form, or replied yes. The workflow that stays clean:

1. For a cold, skip-traced lead, send the **first** text yourself from JARVIS's queue (Draft text › Open in Messages).
2. When they reply with interest, enroll them so the engine handles the follow-up:

```powershell
python run.py sms enroll replied_sellers.csv --consent
```

Then set `sms.enabled: true` in `config.yaml` and add the Twilio keys to `.env`. Edit the messages under `sms.sequence`. The first message already includes opt-out wording.

Other built-in guardrails:
- No texts outside 9 AM–8 PM.
- A daily cap.
- Every "STOP" or "wrong number" reply goes to `data\dnc.txt` and is never texted again.
- Any other reply stops that seller's sequence and lands in `out\hot_replies.csv` so you call them.

## 7. Connect to Claude Desktop (voice/chat control)

Open `%APPDATA%\Claude\claude_desktop_config.json` (create it if missing) and add the engine, using your real paths:

```json
{
  "mcpServers": {
    "jarvis-engine": {
      "command": "C:\\JARVIS\\jarvis-engine\\.venv\\Scripts\\python.exe",
      "args": ["C:\\JARVIS\\jarvis-engine\\mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. Then you can ask things like:
- "How many new leads came in today?"
- "Add Polk County, FL."
- "Run the scraper for Hillsborough."
- "Any replies from sellers?"
- "Show me today's feed matches."

## 8. Hands-free voice ("Hey Jarvis")

**What you need:** a microphone (a USB conference mic or a decent headset works best in a noisy room) and speakers.

1. Install the voice add-on (one time), inside the same activated venv:

```powershell
pip install -r requirements-voice.txt
```

2. Start it:

```powershell
python run.py voice
```

The first start downloads the wake-word and speech models, so it takes a minute. After that:

- Say **"Hey Jarvis"**. You'll hear a chime. Then speak normally, for example:
  - "Add a lead at 4120 Elm Street, Pinellas, seller Linda, asking one eighty, call her Friday."
  - "No answer on Elm Street."
  - "Who do I call next?"
  - "How far am I from fifty K this month?"
  - "Any new county leads in Pasco?"
  - "Run the numbers: ARV two fifty, fourteen hundred square feet, medium rehab."
- **Follow-ups:** after Jarvis answers, keep talking within about 6 seconds and you don't need to say the wake word again.
- **Ending:** say **"that's all"** or **"stand by"**, or stay quiet, and Jarvis goes back to waiting for "Hey Jarvis."

**Voice:** It speaks with your ElevenLabs voice (`elevenlabs_voice_id` in `config.yaml`), using `ELEVENLABS_API_KEY` from `.env`. If ElevenLabs is unreachable or out of credits, it falls back to a free British voice (en-GB-RyanNeural), then the built-in Windows voice. Each reply uses ElevenLabs characters from your plan.

**Speed:** speech streams: ElevenLabs audio starts playing as it arrives, the next sentence is made while the current one plays, and "On it, boss" is pre-made so it plays the instant you finish talking while Claude works. The voice window prints how long each reply took (`timing: you stopped → text 0.6s · first word 1.2s`). Settings under `voice:` in `config.yaml`:
- `end_silence: 0.9` seconds of quiet that mean you're done talking. Lower is snappier; raise it to 1.2 if it cuts you off mid-sentence.
- `model: claude-haiku-5-5` uses a faster, cheaper model for voice only (default: `anthropic_model`). Quicker answers, slightly less careful.
- `whisper_model: tiny.en` transcribes faster than `base.en` but mishears more.
- `elevenlabs_latency: 3` (0-4): higher starts speaking sooner.

**Engine email (AI Sidekicks):** optional, so campaign emails send and replies are read even with Claude closed. One time: in console.cloud.google.com create a project, enable the Gmail API, set up the OAuth consent screen (External, add your Gmail as a test user, then publish it "In production" so sign-in doesn't expire weekly), create an OAuth client ID of type **Desktop app**, and save the JSON as `data\google_client.json`. Then run `.venv\Scripts\python run.py gmail-auth`, sign in in the browser, and restart JARVIS. Settings → Engine email shows the account and turns it on or off.

**PC link:** the JARVIS app talks to this engine through a small server the Claude desktop app runs. It's down while Claude is closed or the PC sleeps, and it reconnects on its own when they're back. After updating engine files, run Stop-JARVIS → Start-JARVIS and fully quit and reopen Claude (the link server only loads new code when Claude starts it).

**Troubleshooting:**
- **It doesn't hear you:** run `python run.py voice --list-devices`, find your mic's number, then start it with `python run.py voice --device <number>`.
- **It wakes up by accident:** raise `wake_threshold` to 0.6 or 0.7. If it misses you, lower it to 0.4.
- **It mishears words:** set `whisper_model: small.en`. It's more accurate but a bit slower.

**Cost and privacy:** Wake-word detection and speech-to-text run locally on your PC, so nothing is sent until you say "Hey Jarvis." Each request then uses your Anthropic API key, which typically costs a fraction of a cent to a few cents.

**Start with Windows:** create a second scheduled task, the same way as the loop:

```powershell
schtasks /Create /TN "JARVIS Voice" /SC ONLOGON /TR "C:\JARVIS\jarvis-engine\.venv\Scripts\python.exe C:\JARVIS\jarvis-engine\run.py voice"
```

## 9. PC link (app ↔ engine sync)

Voice changes are saved on your PC. To get them into the JARVIS app (and your phone):

1. Make sure the engine is added to Claude Desktop (section 7).
2. Open the JARVIS artifact **inside the Claude Desktop app**. Allow it to use **jarvis-engine** when asked.

A green **PC link** appears under the goal bar. While JARVIS is open on your PC, it syncs every 15 seconds:
- Voice changes flow into the app and up to your phone.
- Phone and app changes flow down to the PC, so Jarvis knows about them.

**When the app isn't open on the PC:** Voice changes wait on the PC and sync the next time you open JARVIS in Claude Desktop. Anything you do on your phone reaches voice-Jarvis the same way, the next time JARVIS is open on the PC.

**Approval prompts:** Claude Desktop may ask you to approve the "save" sync call. If it offers to always allow, choose that.

## 10. HQ in VR (Meta Quest 2)

Double-click **Start-HQ-VR**. On the Quest, open the address it shows in Meta Quest Browser, then enter the 6-digit pairing code. In HQ, press **Enter VR**. Full setup, controls, security and the test checklist are in **VR.md**. VR hasn't been tested on a physical headset yet.

## 9b. Morning brief

One read across both businesses: seller follow-ups and tasks due, new county leads, seller texts, AI Sidekicks replies (and who wants a call), calls today and tomorrow, intro emails going out today, approvals waiting, and anything that failed. It only reads; it changes nothing.

- In the app: top of **Today** (MolaBucks) and **AI Sidekicks Home**. Refresh re-reads it.
- By voice: "Hey Jarvis, morning brief."
- On your phone: a scheduled task in Claude reads it every morning through the PC link and sends you a notification. The PC needs to be on with the Claude desktop app open.
- Engine op: `hq.morning_brief` (read channel, `crm_pull`). Test: `python tests/test_morning_brief.py`.

## 9c. CRM plans (AI Sidekicks)

When a prospect says yes, open them in the app and press **CRM plan**. The Solutions Architect writes the plan for a custom CRM built around how they run: records (owners, properties, units, tenants, leases, work orders, leads), pipelines, automations, screens, integrations (marked "to confirm" unless they told you they use it), build phases, and open questions for the call. You review it before anything is built. Client projects have it under Agent work too.

## 9d. County leads: past auctions, owners, call logging

- **Past auctions drop off by themselves.** Once a day the engine sets aside county leads whose sale date has passed (status `expired`). The review list, the morning brief, the CSV export and "Hey Jarvis, any new county leads?" only show sales that can still be worked. If the county postpones a sale and the scraper sees it again with a later date, it comes back.
- **Owner lookup (Pinellas).** After the daily scrape, the engine looks up each upcoming Pinellas lead on the Pinellas County Property Appraiser's public site (pcpao.gov): owner name(s), mailing address, use, year built, living area, just/market value and the appraiser's sales-comparison estimate. Up to 80 a day, 2 seconds apart, each lead once. It flags facts that often mean a motivated owner: an estate ("SMITH, JOHN EST"), several co-owners sharing a family name (possible heirs), a trust, a mailing address elsewhere, an out-of-state owner. Owners also go into The Hunter, so AvaData skip-trace exports carry the name and mailing address, which is what skip tracing matches on. Run it now with `python run.py owners`. Other counties aren't supported yet. Phone numbers still come from skip tracing (AvaData), which is paid.
- **Log calls by voice.** "Hey Jarvis, log a call with the Doe estate, interested, call back Friday." JARVIS finds the deal by name or address. If it's only a county lead so far, he adds it to the pipeline first (owner as seller, sale date and amount owed in the notes). If two match, he asks which. Follow-up dates must be ones you actually said.

## 9e. Pre-foreclosure, probate and evictions (Pinellas Clerk)

Every morning, after the scrape and owner lookup, the engine reads the Pinellas County Clerk's free daily data files (publicfiles.mypinellasclerk.gov, no login, no CAPTCHA) and puts what it finds in The Hunter:

- **Pre-foreclosure (lis pendens).** New residential mortgage foreclosure cases (homestead and non-homestead; commercial and land are left out). The property is the address where the tenant/occupant was served, or a defendant's own Pinellas address, and it must check out on the property appraiser's site. Owner(s) and mailing address come from the appraiser. Notes: lender, homestead or not, the claim range from the case type (the file has no loan balance), defendants, case number.
- **Probate.** New estates. The decedent's name is searched on the appraiser's site, and a parcel is only kept when it's clearly theirs: owner marked as an estate, the petitioner on title with them, or a full-name match that isn't one of several. Common names with no tie-breaker are skipped, not guessed. Notes: date of death, petitioner (personal rep), attorney. The file has no address for the petitioner.
- **Evictions (tired landlords).** New residential evictions and unlawful detainers. The rental must check out on the appraiser's site; institutional landlords (Progress Residential, apartment funds, etc.), apartment complexes and mobile-home lots are left out. Notes say whether the owner filed it or a manager did. Tenant names are not stored.

The first run looks back 30 days. It asks the appraiser at most 250 times a run (1.5 s apart), caches every answer, and keeps the files in `data\clerk\`, so each day after that only costs the new filings. Run it now with `python run.py clerk` (or `--category probate`). Settings in config.yaml under `clerk_records:` (`enabled`, `days_back`, `max_lookups`, `pause_s`).

Probate and eviction leads land in The Hunter's **Review** tier on purpose: they're sensitive records, so look before reaching out. Phone numbers still come from skip tracing (AvaData). Other counties aren't connected yet.

## 9h. AI Sidekicks business CRM (milestone 1)

One record per business in JARVIS Engine, shown in the app under AI Sidekicks → Prospects → Business CRM.

- **Dedupe.** Same website domain, business email, phone (last 10 digits) or name + city = the same business. New details from another source are added to it, never as a second record.
- **Sources.** Every fact has a grade (verified, public, third-party, inferred, invalid) and the page it came from. Emails: "published, unverified" when found on the business's own site and its domain accepts mail (free DNS check); "third-party" when found on Facebook, Yelp or a directory; "invalid" when the domain takes no mail. "Verified" is reserved for a paid verifier (none connected). Inferred emails are never used.
- **Score.** 0–100 from industry, size, evidence of inquiries/scheduling, admin load, contactability, decision maker and source quality, each with its reason and evidence strength. Priority: High, Medium, Low, Research required, Disqualified. Industry alone scores little.
- **Pipeline.** 17 stages, Newly Discovered → Won/Lost/Disqualified. The SDR and the system move a business only on recorded evidence (sent email, real reply, booked meeting, agreement sent). You can move one by hand with a note, saved as the evidence. Every move is in the stage history.
- **Feeds.** Every 30 minutes the SDR adds new discoveries and campaign prospects and advances stages; every 15 minutes it reads up to 10 new websites (free) so scores rest on what the site says. "Copy my app prospects in" brings the app's existing prospects over once (duplicates merge).

- **Research on demand.** `hq.di_research` queues the SDR to find a published email and sourced facts for every "Research required" business (or the ids you name). Results save onto the record by themselves and rescore it. About $0.04-0.05 per lookup, inside the SDR's budget.
- **Honest scoring.** Negated words don't count as evidence ("no booking software found" isn't a booking signal). A Gmail or Verizon address published on the business's own site counts as their published email. A bounced email marks the contact invalid.

## 9i. First paying client: outreach rules, proposals, demo pages

- **Claims check (every outreach draft and every send).** No superlatives or rankings (best, only, #1, leading), guarantees, multiples (10x, double your), percentage results, or "our clients see". A draft that has one is sent back to the AI once with the problem named; if it still has one it's held back. A send can't be queued with one at all.
- **Follow-up rules.** A sequence stops on any reply (auto-replies included), an opt-out or a bounce. Each follow-up must add something new: #2 leads with a new example tied to a true fact from the CRM record, #3 is a plain last note. "Just checking in", "circling back", guilt or pressure lines, and repeating the first email are rejected.
- **Bounces.** The reply checker records "address not found" notices; no follow-up or new email goes to that address and anything queued to it is cancelled. Record one by hand with `hq.email_bounce_add`.
- **Reply-rate reference.** Campaign stats show the bounce rate and a sourced reference (Hunter, State of Email Outreach 2026: 4.5% average reply rate, 6.8% for three-message sequences). It's a reference, not a promise; under about 100 sends, zero replies isn't evidence yet.
- **Discovery to proposal.** Paste the call notes or transcript: `hq.di_proposal {company or dc_business_id, notes}`. The account executive maps how they run today, the bottleneck in their words, the smallest solution that removes it (real-time vs background, read vs write, what a person approves), phases, a price range and a client-facing proposal. Prices come from your guide (`hq.set_di_pricing`); without one it suggests a range marked "owner to confirm". Level 2: it waits for your review; nothing is sent. Signing stays behind DocuSign approval.
- **Demo page for a booked call.** When an AI Sidekicks call is booked, alongside the pre-call brief, the architect writes a one-page demo for that business (how the assistant answers, 3 sample calls for their real services, handoffs, what we'd set up with them) into `data/demos/`. Fixed template, nothing loaded from the internet, says plainly it's a demo. Up to 4 a day; `hq.demo_page_build` makes one on request.
- **What they use today.** The pre-call brief now detects booking, field-service, CRM and answering-service tools from their site's code (ServiceTitan, Housecall Pro, Jobber, Calendly, Ruby, Smith.ai...) and has a "current setup" section with sources. It's no longer property-management-only.

## 9j. Talk to any employee: phone (Telegram), their own voices, live calls

**One brain** (`engine/employees.py`): ask any employee (SDR, AE, Marketing, Architect, Hunter, ...) in plain words. They
answer from the real records and act in their own lane: look up, research, build lists, draft. They never send, book,
launch, approve or spend on their own; anything like that waits for your approval. Every call is priced and held to the
daily AI cap (Haiku, a cent or two per question).

**Phone line (Telegram)** (`engine/telegram.py`):
1. In Telegram message @BotFather, send /newbot, choose a name and a username ending in "bot". Copy the token.
2. Add `TELEGRAM_BOT_TOKEN=<token>` to `.env` on this PC (never paste it into chat). Restart JARVIS.
3. JARVIS app > Settings > Phone line shows a pairing code. Send `/start <code>` to your bot. Only that chat is served.
Then: "SDR: find 20 roofers in Lynn", "AE: prep me for Lakeview", or any question (JARVIS routes it). "approvals" lists
what's waiting; "approve <id>" / "reject <id>". Updates arrive by themselves: replies, meetings booked, approvals,
work you asked for when it's done, failures, and an 8:30 summary. Quiet 9 pm to 8 am (hot replies still come). "mute".

**Their own voices**: `voice.agent_voices` in config.yaml gives each employee an ElevenLabs voice. Say "Hey Jarvis,
ask the SDR who replied today" and the SDR answers in its own voice.

## 9k. Live voice calls with an employee (`engine/voice_bridge.py`)
1. Install the tunnel: `winget install --id Cloudflare.cloudflared` (free; no account needed for this kind of tunnel).
2. `python run.py voice-calls` creates one ElevenLabs voice agent per employee (uses ELEVENLABS_API_KEY, your plan's minutes).
3. Restart JARVIS. It starts the bridge (127.0.0.1:8765 only) and the tunnel, and points the agents at it.
4. Text your bot "call" or "call sdr" for your private call link; open it on your phone and tap Start call.
Security: the call link carries a private key (keep it private; it changes only if you delete data/voice_bridge.json);
the agents require a signed session, so the ElevenLabs agents can't be called without that link; the voice agents reach
JARVIS with a separate key. The tunnel address changes when JARVIS restarts, so ask the bot for a fresh link then.

## 9g. Standing duties: the workforce works without being asked (Stage 2)

Every AI Sidekicks agent now has a standing job on a schedule. Duties create ordinary HQ tasks, so approvals, retries, history and spend limits all still apply. A restart never runs a duty twice.

| When | Agent | Duty |
|---|---|---|
| 03:00 daily | (system) | Database backup + restore test (opens, integrity check, same rows); last 14 kept |
| 07:30 daily | QA | System check: engine heartbeat, email and reply checker, county scrape, backup, data integrity, failed tasks, AI spend. Problems show in the morning brief |
| 09:00 daily | SDR | Find 25 businesses, rotating: home services, property management, auto repair, cleaning and restoration (Greater Boston) |
| every 30 min | SDR | Look up published emails for a finished discovery (company sites first; never guessed) |
| 10:00 daily | SDR | Draft follow-up #2/#3 for anyone due, up to 25 a day. Sending still needs your approval |
| Mon 09:30 | Marketing | Next week's content plan: 3 LinkedIn posts, 3 short-video scripts, 1 carousel, comment-a-word CTAs, real features only |
| 17:30 daily | PM | Blockers: failed or stuck tasks, approvals waiting over a day, missed deadlines |

- **Budgets.** Per-agent daily AI budgets inside the overall cap: SDR $2.50, Marketing $0.30, AE $0.20 (pre-call briefs and proposal drafts share it; one proposal can cost up to about $0.16). Over budget, that agent's tasks wait until tomorrow. Change them from the app (`hq.set_ai_budgets`).
- **Honest states.** An agent with nothing to do shows "idle, monitoring" and what it watches. If JARVIS Engine stops ticking for 3 minutes, every agent shows "offline".
- **Settings** in config.yaml under `hq:` → `prospecting:` (`industries`, `areas`, `per_day`, `followups_per_day`, `enabled`), and `duties: false` turns all duties off.
- **It only runs while your PC is on and JARVIS is running.** Set Windows sleep to Never and add Start-JARVIS to startup so it runs around the clock.

## 9f. Pre-call brief (AI Sidekicks)

Before a sales call, JARVIS puts one page together on the property manager: who they are, how many doors they claim, the property software their tenant portal runs on (AppFolio, Buildium, Yardi...), whether they advertise a 24/7 line, live chat or an online maintenance form, what public reviews say about calls and maintenance (with links), the email we sent them and any reply (auto-responders are marked as such), likely pain points, 4 questions to ask and the angle to pitch. Anything it couldn't confirm is listed as unknown.

- **Automatic:** for every AI Sidekicks meeting in the next 36 hours, and as soon as a reply reads as "interested" or "wants a call". The morning brief marks those calls "brief ready".
- **On demand:** "Hey Jarvis, brief me on Harbor Point", or `python run.py brief Harbor Point` (a website works too: `python run.py brief harborpoint.com`).
- **Cost:** about 2 to 5 cents each (fast model plus up to 3 review searches), inside the daily AI cap. At most 8 automatic briefs a day. Nothing contacts the prospect.

## 8b. Voice: stability

- **Every request starts with "Hey Jarvis".** He no longer keeps listening after answering, so he won't reply to other voices, a video or another assistant. Set `followup_without_wake_word: true` in config.yaml under `voice:` to get follow-ups back.
- **Short answers.** He speaks a couple of sentences at most; the full answer shows in the JARVIS app.
- **No error loops.** If a request to the AI fails, he puts the conversation back (or clears it, if it was the cause), says what happened once, and goes back to waiting for "Hey Jarvis". The conversation is checked before every request, so he can't get stuck on the same "bad request".

## Ground rules the engine follows

- **Public records only.** It reads public auction calendars and record pages.
- **Paced requests.** It waits between page loads (`delay_seconds`).
- **No CAPTCHA bypassing.** If a site shows a CAPTCHA or blocks automated access, it logs that and skips the site. Use that county's official data download or bulk records request instead.
- **No invented data.** Claude only copies values that are on the page. Missing fields stay blank.

## Files

| Path | What it is |
|---|---|
| `counties.yaml` | Your county sources |
| `config.yaml` | Schedule, texting rules, message sequence |
| `data\jarvis.db` | Everything the engine has found and sent (SQLite) |
| `out\` | Import files, skip-trace files, hot replies, feed matches |
| `engine\adapters\` | One file per site type; add your own here |
| `voice\jarvis_voice.py` | The hands-free assistant |

## 11. JARVIS 2.0: two businesses and the AI workforce
See `JARVIS2.md` for the full plan, phase status and architecture review.
- **AI agents:** they use `ANTHROPIC_API_KEY` from `.env`. Fast work runs on `claude-haiku-5-5` and heavier work on `claude-opus-5-5`. Every call is priced before it runs, against a daily cap ($5 by default). Change the cap in the JARVIS app under Settings → AI spend & cap, or in the Executive Overview. At the cap, agents stop calling the AI and their work waits until tomorrow.
- **What agents do:** they draft and analyze from the record you start them on. Controlled (level 2) drafts need your Accept before they can be used. Nothing external goes out without your approval.
- **System check:** Settings → System check runs read-only integrity checks on your records and on this engine's database (`hq.integrity`).
- **Tests:** `python tests/test_hq.py` covers the queue, approvals, businesses, AI work (with a fake client, no real calls), the spend cap, review, delegation and integrity.
- **3D people (HQ):** the files live in `web/chars/` next to `web/jarvis.html`: one `.glb` per person, `player.glb` for you, `clips.json` for the animations and `credits.txt` for the licences. Keep the folder when you update `jarvis.html`. If it's missing, HQ shows the simple figures instead.
- **Outreach email (AI Sidekicks):** emails go out from your own Gmail through the claude.ai Gmail connector, one at a time, only after you approve the exact email. The approval is recorded in this engine, so sending needs the PC link. Mark a prospect **Do not email** and the engine refuses anything to that address.
- **AI Sidekicks sales loop:** replies are read from Gmail and labeled by the SDR ("asked to stop" means do-not-email). Up to two follow-ups go out, only with no reply. Calls are booked on Google Calendar, agreements sent from your DocuSign template, and demo phone agents built in ElevenLabs only for companies that said yes. Each of these waits for your approval in this engine. Web prospect search costs $0.01 per search inside the daily AI cap.
- **Outreach campaigns (AI Sidekicks):** ask the SDR in HQ, or Prospects → New campaign. The SDR drafts one email per company; you review them all on one screen and approve once. Up to your daily limit (default 20) go out on weekdays 9–5, a few minutes apart, only while JARVIS is open with the PC link on. Every email carries the footer you set (name and mailing address). Voice works too: "Hey Jarvis, tell the SDR to find 100 property management companies in Massachusetts", then "draft intro emails for them"; you approve the emails in the app. Change the sending hours with `outreach_window: {days: [0,1,2,3,4], start: 9, end: 17}` in `config.yaml` (Mon=0).

## 12. AI Sidekicks Instagram assistant (@aisidekicks)
The Instagram DM assistant for AI Sidekicks' own account: it answers questions about AI Sidekicks' services and turns interested people into discovery-call requests that Mola approves. Code in `engine/igassist/`, data in its own file `data/clients/aisidekicks.db` (git-ignored; never push it). Double-click `Start-Sidekicks.bat` (dashboard at http://127.0.0.1:8600; a browser page creates your login the first time). Nothing is sent to Instagram until you approve the pilot and turn sending on; calendar entry is manual. Setup, Meta steps and safety rules: **AISIDEKICKS.md**. Tests: `python -m tests.test_igassist`.
