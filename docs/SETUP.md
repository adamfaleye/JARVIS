# JARVIS Engine

This is the part of JARVIS that keeps working when the app is closed. It runs on your Windows PC and:

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
