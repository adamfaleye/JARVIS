# JARVIS 2.0: Phase 0 audit, architecture and plan

This file covers Phase 0 of the JARVIS 2.0 spec: an audit of what exists today, the architecture I'm proposing, the risks, the decisions that need the owner, and the migration plan. I wrote it from the actual code, not from memory. Each phase below gets a status line as it's done.

## 1. What exists today (inventory)

### 1.1 Frontend: the JARVIS app
- **Stack:** vanilla JavaScript, with no framework. `build.py` joins `src/*.js`, `head.html` and `body.html` into one `jarvis.html` of about 850 KB and about 8,000 lines.
- **Where it runs:** it's published as a claude.ai artifact. Runtime capabilities used: `db` (account storage), `user`, `sample` (Claude), `mcp` (connectors plus the PC link), `assets`, `downloads`.
- **The core:** the ops layer (`03-ops.js`). Every write is a `defOp` with validation, a role check, confirmation for consequential actions, an idempotency key and an audit entry. Screens, the assistant and voice all call the same ops.
- **Screens:** Today, HQ (3D), Dashboard, Inbox, Pipeline, Call Queue, Tasks & Calendar, Hunter, Campaigns, Templates, Call Log, Numbers, Buyers, Realtors, Do Not Contact, Import, Deal Analyzer (comps rules plus Zillow), Contracts, Automations, Activity, Settings, Learn.
- **3D:** Three.js r128 loaded from a CDN. It's a first-person HQ office with HDR bloom, quality presets and day/night, a HUD, minimap, live feed and fast travel. There's a WebXR mode for Quest 2, verified in an emulator only. The 3D view is optional, and every action also exists in the Operations view and the regular screens.
- **Roles:** team mode has owner, manager, acquisitions, dispositions and viewer roles (`02b-team.js`). The owner is the default when solo.

### 1.2 Backend: JARVIS Engine (Python 3.11 and SQLite on the owner's PC)

| Module | Purpose |
|---|---|
| `engine/hq.py` (934 lines) | Agents, durable task queue (leases, dependencies, retries, recovery), hash-bound approvals (level 3 can't run until `approved_hash == params_hash`), goals, plans, briefings, end-of-day review, pause/emergency stop, health, away report |
| `engine/hunter.py` (1,399 lines) | County sources, raw records, properties, owners, lead events, scoring, skip-trace batches, review queue |
| `engine/outreach.py` (956 lines) | Campaigns, enrollments, DNC, sender numbers, dialer, usage |
| `engine/agent.py` | Texting reply agent: drafts only, sends only from the Inbox after gates |
| `engine/crm.py` | Mirror of the app's CRM (`crm_items`); the newest `_ts` wins |
| `engine/sms.py` | Twilio sequence, STOP handling, quiet hours, `require_consent: true` |
| `engine/web.py` | HTTPS server for the Quest and other browsers, paired-device tokens |
| `mcp_server.py` | The PC link: `crm_pull` (read-only) and `crm_push` (writes) |
| `voice/jarvis_voice.py` | Hands-free "Hey Jarvis": openWakeWord → faster-whisper → Claude tool loop → edge-tts, pyttsx3 or ElevenLabs |

- **Database** (`data/jarvis.db`): leads, contacts, messages, feed_deals, runs, kv, crm_items, the hq_* tables, the hunter_* tables, and the outreach tables. Every schema change so far has been additive (`CREATE TABLE IF NOT EXISTS`).
- **Authentication:** the app inherits the claude.ai sign-in. The engine trusts the PC link channel. The web server requires a paired-device token.

### 1.3 Integrations, verified against how they're actually used

| Integration | State |
|---|---|
| Claude, in the app | Through the account's `sample` capability. No key in the browser. |
| Claude, in the engine | `ANTHROPIC_API_KEY` in `.env`, used by voice, the reply agent and page reading |
| Twilio | Code complete (texting, dialer, numbers). Not live: needs funds, a number and A2P registration. |
| ElevenLabs | Connector. Demo phone agents for AI Sidekicks prospects who said yes (each one approved by you), and monthly call counts for clients' live agents (Phase 10). Optional TTS voice for the desktop assistant. |
| Zillow | Connector, used by the Analyzer. Real calls verified. |
| Gmail | AI Sidekicks outreach email sends through your Gmail connector: single emails each approved by you (Phase 9), or campaign batches you review and approve once, sent paced on weekdays (Phase 11). |
| DocuSign | Connector. AI Sidekicks agreements send from the template you pick in Settings, each one approved by you; signature status is read back (Phase 10). Your account has no usable template yet. |
| Google Calendar | Connector. Finds your open times and books AI Sidekicks discovery calls with a Meet link, each one approved by you (Phase 10). |
| Supabase, n8n | Not used. Nothing to preserve. |

- **Deployment:** the artifact on claude.ai (version 27). The engine runs on the owner's PC through Start-JARVIS. Start-HQ-VR serves the headset. There's no always-on host yet.

### 1.4 Tests that exist
- **App:** 15 jsdom suites, about 330 checks: acceptance, acq, county, hq, hunter, inbox, outreach, pcvoice, rehab, team, today, zillow, and others. There's also a 48-check WebXR end-to-end test.
- **Engine:** test_hq, test_outreach, test_hunter and test_web.

## 2. Assessment against the JARVIS 2.0 spec

| Spec area | Today | Gap |
|---|---|---|
| Preserve MolaBucks | All present and tested | Keep every op and record path unchanged. |
| Controlled autonomy, enforced on the backend | Levels 1 and 3 enforced in the engine with hash-bound approval; read and write channels split | Add level 2 ("controlled"). Tag tasks by business. |
| Agent orchestration | Durable queue, dedupe, leases, retries, timeouts, audit (`hq_events`), no false completion (verified results) | Registry fields (division, department, role, tools, model, permission level). Delegation by department. Agents beyond MolaBucks. |
| Two businesses | One business, with a demo workspace | Business switcher, separate AI Sidekicks collections, ops scoped to a business, separate audit, a combined overview. |
| AI Sidekicks OS | Doesn't exist | New workspace: prospects, clients, projects, tasks, tickets, QA, deployments, billing, the flagship product. |
| Executive command center | Today and Dashboard for MolaBucks only | Combined overview from real records. |
| Command interface | Assistant (typed) plus desktop voice | Make it aware of the active business and refuse cross-business actions. |
| Third-person open-world city, two towers | First-person single office | Third-person controller, procedural city, two towers with interiors, traffic, pedestrians. |
| Human employees, custom executive avatar | Stylized robots | Rigged human models and animations. This needs assets; see the decisions in section 5. |

## 3. Proposed architecture: keep the stack and add layers

I'm keeping the current stack, which the spec allows: "inspect before selecting technologies; reuse what works." Rewriting the app in React, or moving it to Supabase, would put working, tested features at risk for no gain in function.

| Layer | Where | Change |
|---|---|---|
| 3D presentation | `07f/07g` plus new `07h-city.js` | Third-person city with two towers. The office interiors reuse the current HQ engine. Lazy-loaded, and it never blocks business screens. |
| Frontend app | `src/*.js` | Business switcher. AI Sidekicks screens. Executive overview. |
| Business logic | Ops layer | Every op declares `biz` ("molabucks" or "demigod"). AI Sidekicks ops write only to `demigod/*` collections and to their own audit log. |
| Agent orchestration | `engine/hq.py` | One registry for both businesses. Tasks carry `division`. Assignment across divisions is refused. Levels 1, 2 and 3. Delegation by department. |
| Tool execution | Engine executors | Each agent gets real executors. Research and drafting agents call Claude from the engine; the key stays on the server. |
| Persistence | Account `db` plus engine SQLite | Separate collection paths per business: `data/users/<id>/demigod/<kind>`. MolaBucks paths stay as they are. Engine changes are additive columns only, with a backup taken first. |
| Auth and permissions | claude.ai identity, team roles, engine channel split, device tokens | Business-scoped op checks, plus a business tag on every audit entry. |

**Data separation rules:**
- AI Sidekicks records live only in AI Sidekicks collections, and every record carries `biz`.
- The assistant may only use the active business's ops.
- Every audit entry is written to that business's own log.
- The executive overview reads both businesses but writes nothing.

**Task status mapping** (spec → engine):

| Spec status | Engine status |
|---|---|
| Created / Assigned | `queued` |
| Accepted / In Progress | `running` (leased) |
| Awaiting Information | `blocked` or `waiting` |
| Awaiting Approval | `awaiting_approval` |
| Completed | `completed` (verified) |
| Failed | `failed` |
| Cancelled | `cancelled` / `rejected` |

The UI shows the spec's names.

## 4. Technical risks

1. **Realistic human avatars, and one that looks like the owner.** This needs rigged 3D models and an animation set. Procedural shapes won't pass as human. See the decisions in 5.
2. **Performance.** An open city, two towers, crowds and traffic in a browser (and a Quest) means instanced low-poly crowds, level of detail and streaming. Budgets get measured the same way they were for VR.
3. **Agents need real capabilities.** Research, proposals and QA analysis need the model to run on the engine (paid per use). Web research needs a search tool, which costs extra.
4. **Always-on operation.** Agents stop when the PC sleeps. The cloud server is still pending.
5. **The artifact's 16 MB page limit.** Models and textures go in the artifact's asset store and load lazily.

## 5. Decisions and costs the owner needs to know about first

| Item | Why | Options |
|---|---|---|
| Executive avatar | "Based on my actual appearance" | (a) Make a free avatar from a selfie with a tool that exports GLB, and send me the .glb. (b) Send a full-body photo; I build the avatar infrastructure with a placeholder until a matching model exists. Paid custom modeling is optional. |
| Employee and executive animations | Walk, run, idle, sit, type, talk | Mixamo (free, but needs your Adobe sign-in to download; I can't download it from here), or the CC0 animation packs that install through npm. |
| Agent AI usage | Research, drafting, QA analysis on the engine | Billed to your Anthropic API key per use. I'll show an estimate before turning on scheduled agent work. |
| Web research tool | Prospect and competitor research | Claude's server-side web search (pay per search), or start without it and use only records you supply. |
| Always-on host | Agents running 24/7 | Small cloud server, about $5–12 a month (already planned). |

Nothing in this list gets turned on without your yes.

## 6. Migration plan and phase order
- **Phase 1, business foundation:**
  - Engine: registry fields; `hq_tasks.division` (additive column, database backed up first); cross-division refusal.
  - App: business switcher, AI Sidekicks collections and ops, executive overview, business-aware assistant.
  - MolaBucks data isn't moved or rewritten.
- **Phase 2, third-person world:** city, two towers, entrances, interiors, traffic, pedestrians, third-person controller. The current HQ office becomes a floor inside the MolaBucks tower.
- **Phase 3, human workforce:** avatar system with placeholders until real models arrive; profiles; workstations; status driven by real tasks.
- **Phase 4, agent operating system:** executors for every role; delegation by department; level 2; approval queue and monitoring across both businesses.
- **Phase 5, AI Sidekicks OS:** tickets, QA, deployments (approval-gated), billing, and the flagship property-management product.
- **Phase 6, executive operations:** command interface, combined reporting, notifications.
- **Phase 7, hardening:** permission tests, integrity checks, monitoring, documentation.

Every phase ends with the full app and engine test suites passing, before anything is published or installed.

## 7. Phase status
- [x] Phase 0: audit and architecture (this file)
- [x] Phase 1: business foundation. Engine: one registry with standard identity fields (`engine/org.py`); every task carries its business, with a backup taken before the additive migration; cross-business assignment refused by the engine; agents without executors reported as not operational. App: business switcher, separate AI Sidekicks collections, ops and activity log; prospects, clients, projects and tasks; Executive Overview built from records; a JARVIS assistant aware of the active business. Tests: engine test_hq has 14 new checks; app test/business.js has 29 checks; all earlier suites pass.
- [x] Phase 2: third-person world. HQ now opens on the street (`07h-city.js`), in Founders Plaza between the two towers.
  - **City:** a 7×7-block street grid with lane markings, crosswalks, streetlights and trees. Traffic runs on every road and stops at signals on every crossing, and cars brake for you. Pedestrians walk the sidewalks and the plaza. Day and night follow your local time.
  - **MolaBucks Capital tower:**
    - Deep green glass with gold mullions.
    - A gold crown drawn from the twin roof peaks in the logo, with the logo itself (unaltered) near the top.
    - The crown beacon lights only while approvals are actually waiting.
    - The entrance opens onto the existing operations floor.
  - **AI Sidekicks tower:**
    - Octagonal, with violet light lines.
    - The entrance opens onto a new agency floor with one workstation per role: Sales, Delivery and Client Success, nine in all.
    - Desks show the engine's status, which is "not operational" until Phase 4. The two wall boards come from your AI Sidekicks records.
    - The desks are empty until Phase 3.
  - **Third person everywhere:**
    - The stand-in executive avatar wears a black suit with gold accents, and you can adjust skin tone, hair and beard under the gear icon.
    - The camera collides with walls and ceilings. V switches the MolaBucks floor back to first person.
    - VR still uses the MolaBucks floor only. Entering VR from the street takes you there.
  - **Moving between scenes:** E at a door; the Tab map lists both towers. Objectives and "Go to" route through the right building, and HQ remembers where you were.
  - **Honesty:** traffic and pedestrians are scenery and never stand for agents, leads or activity.
  - **Tests:** a 30-check browser test covers walking, collision, camera, traffic that stops for you and doesn't run reds, both entrances and exits, AI Sidekicks desk status, cross-building travel and remembered location. All earlier app and engine suites still pass.
- [x] Phase 3: human workforce.
  - **Everyone is a person (`07i-people.js`):** each AI employee on both floors is now an original character at their workstation, dressed for the role. That's the six MolaBucks staff and the nine AI Sidekicks roles. Robots are gone. JARVIS stays the holographic core in the CEO suite.
  - **Posture comes only from the engine's task state:**
    - working: typing
    - waiting for your approval: hand raised
    - failed or blocked: slumped
    - idle: sitting back, looking up when you approach
    - not operational: hands in lap, so nobody looks busy without a running task
  - **Your avatar:** matched to your photo (skin tone, short hair, goatee), black suit and tie with gold accents.
  - **Profiles:** each employee's sheet shows the engine's identity fields (business, department, role, permission level, AI model tier, tools, output, task types).
  - **AI Sidekicks:** your logo is now on the tower, lobby wall, flag, header and switcher, and the theme follows its navy and blue, with the green online dot.
  - **Shapes:** the figures are built from simple geometric shapes, with each person's parts merged so the floors stay fast. Phase 8 replaced them with rigged people; the shapes remain only as the fallback.
  - **Tests:** browser checks confirm every employee is human, each posture matches its engine state, and the AI Sidekicks roles show as not operational.
- **Owner decision (Nov 7, 2026):** agents may use his Anthropic API key, with a $5/day cap to start (Phase 4 enforces it in the engine).
- [x] Phase 4: agent operating system (`engine/agents_ai.py`, app `07j-agentos.js`).
  - **Real work for every role:** each role in the roster now has at least one task type that actually runs. Executive Office: Research brief, Chief of Staff workload check. AI Sidekicks: SDR prospect brief and outreach drafts, AE proposal with pricing, Marketing content, Architect requirements, Builder build plan, QA test plan, PM project plan, Account Manager client report, Support ticket triage.
  - **Models and spend:** fast work runs on Claude Haiku 5.5 and standard work on Claude Sonnet 5.5, billed to your key.
    - Every call is priced before it's made against the daily cap ($5 by default). At the cap no call is made, and the task waits until after midnight.
    - Actual token use and cost are logged per call (`hq_ai_usage`) and shown in the Executive Overview and Settings.
    - Only you can change the cap, over the write channel, with confirmation.
  - **Drafts only:** these executors never send, publish, deploy or charge anything. Record text goes to the model inside a data block, with an instruction to ignore any instructions written in it.
  - **When a task counts as done:** the reply must parse and contain the fields its task type needs. If essential facts are missing, the task blocks as "Awaiting information" and lists its questions.
  - **Level 2 (controlled):** the task runs on its own, but its output is held for your review. Nothing from it can be saved to a record or imported until the engine has recorded your Accept. The app re-checks this with the engine before applying anything.
  - **Delegation:** "Ask JARVIS to delegate" (and the command bar) routes a request to the right department by deterministic rules. It never routes to approval-gated work.
  - **In the app:** prospect, client, project and ticket sheets have an Agent work section, and drafts come back to that record. The Executive Office staff (Chief of Staff, Research) now have desks in the CEO suite.
- [x] Phase 5: AI Sidekicks OS (app `06d-dios.js`).
  - **Support:** tickets carry severity and a status flow, and resolving one needs a resolution note. Applying the Support Specialist's triage sets severity and can escalate to you.
  - **QA:** a QA run starts from the QA Engineer's plan, and you record pass or fail per test.
  - **Deployments:** each has a gate. Production needs a passed QA run, the client's recorded approval and no open high or urgent tickets on the project. Approving and marking live are separate steps, each with confirmation, and both are manager-only.
  - **Billing:**
    - Retainer invoices are drafted only for clients with a retainer on file; the rest are listed as skipped.
    - Status goes Draft → Sent → Paid, with Void available along the way.
    - JARVIS records the status but doesn't send invoices or take payment.
  - **Flagship product (PM Suite):** AI Receptionist, Maintenance Coordinator, Leasing Assistant and Operations Dashboard. Each is a delivery template: standard milestones plus tasks for the right roles. You set the prices; none are invented.
- [x] Phase 6: executive operations (app `05d-command.js`).
  - **Command bar (Ctrl/⌘+K):** jump to any screen, open any record in either business, start actions, or ask JARVIS to route a request. The business you have open ranks first, and everything runs through the same ops and confirmations as the screens.
  - **Notifications:** a bell with one list across both businesses, built from real records and engine state. It covers approvals, drafts to review, failed agents, the AI cap, the worker loop, overdue tasks, closings, urgent tickets, overdue invoices, deployments waiting and next actions.
  - **Executive report:** a weekly report for both companies and the AI workforce. Projected money is labeled as projected. Copy it or download it as .md.
- [x] Phase 7: production hardening.
  - **Engine `integrity`:** read-only checks on the SQLite file, stuck leases, the approval gate, verified completions, dependencies, business separation, task types, the AI cap and the worker loop. Planted bad rows are caught and reported, never deleted.
  - **System check in Settings:** checks how records link up across both businesses, invoice and deployment states, deployed projects without client approval, duplicate deals, activity-log separation and business tags. It includes the engine's checks.
  - **Permissions:**
    - Deploy approval, the AI cap, invoice status and product launches are manager-only.
    - Viewers can't create records or hand work to agents.
    - A task can't lower its own approval level through its parameters.
    - Approval, review and delegation need the owner's write channel.
  - **Tests:** engine `test_hq` gained Phase 4 and Phase 7 blocks. The app gained `test/agentos.js` (Phases 4–6) and `test/hardening.js`, and all earlier suites pass.
- [x] Phase 8: rigged people (app `07k-rig.js`, files in `web/chars/`).
  - **Employees:** all 17 roles are now rigged, textured 3D people from the Microsoft Rocketbox library (MIT licence; credits and licence in `chars/credits.txt`). They replace the simple shapes, and each person is picked to fit the role.
  - **Animation:** the Mixamo clips you downloaded (typing, sitting idle, sitting talking, hand raising, standing idle, walking, running, talking) are baked into `chars/clips.json` (about 700 KB). At load time they're retargeted onto each body's skeleton. The bodies are A-pose and the clips T-pose, so the rest poses are lined up first. Clips are shared by every body with the same proportions.
  - **State still drives everything:** working means typing; waiting for approval means sitting with a hand up (legs from sitting, upper body from the hand raise); idle means sitting, and they turn their head to you and talk when you stand at their desk; failed, blocked or offline means head down; not operational means sitting still at slow pace with the screen dark.
  - **Your avatar:** walks, runs and idles by your real movement speed. When rigged, walking is 2.4 m/s so the stride matches the ground, and Shift still runs at full speed.
    - The body is a black suit with the tie recoloured gold. The head is a Rocketbox stand-in, with the hands matched to its skin tone.
    - It's a stand-in, not your likeness, until your own model file arrives.
  - **Loading and fallback:** people load in the background after the floor appears, and only for the floor you're on (about 0.55–0.8 MB each). If the files can't be fetched, the simple figures stay and System check says why.
  - **Engine:** the HQ web server serves `web/chars/*` (plain file names only, cached), so the Quest gets the same people.
  - **Tests:** engine `test_web` checks the route and refuses path tricks. In-browser checks render every person in every state and confirm the floors load rigged people. All earlier app suites pass.
- [x] Phase 9: AI Sidekicks outreach email through Gmail (app `06e-dimail.js`, engine `hq.py`).
  - **Flow:** the SDR drafts the email; on the draft (or a prospect's **Write email**) you edit it, then **Review and send**. The confirmation shows the exact recipient, subject and body.
  - **Approval lives in the engine:** each email is an `sdr.send_email` task at approval level 3. Your Send click creates it and approves it bound to those exact words (hash). The app sends only what the engine's outbox says is approved, then records Gmail's message id back in the engine. No PC link, no send.
  - **Failures:** if Gmail errors, nothing is shown as sent; the approved email stays in the outbox with the error and a **Send now** retry on the prospect.
  - **Do not email:** a per-prospect switch; the address goes on the engine's do-not-email list, cancels anything queued to it and can't be queued again. Every SDR draft keeps an opt-out line.
  - **Logged:** on the prospect (sent history), in AI Sidekicks' activity log and in the engine task history.
  - **Tests:** engine `test_hq` has 9 email checks; app `test/dimail.js` has 18.
- [x] Phase 10: AI Sidekicks sales loop (app `06f-disales.js`, engine `hq.py` and `agents_ai.py`).
  - **Replies:** the app reads the Gmail threads of emails JARVIS sent and records messages from the person emailed (once per message; strangers on the thread are ignored). The SDR reads each reply as untrusted text and labels it: interested, wants a call, question, not now, asked to stop, out of office, other. "Asked to stop" puts them on do-not-email straight away. Checks run every 15 minutes while the AI Sidekicks home is open, or on demand.
  - **Follow-ups:** follow-up 1 is due 3 days after the first email and follow-up 2 at 7 days, only while there's no reply, no opt-out and nothing already pending. The engine refuses a fourth email. The SDR drafts it; you send it in the same Gmail thread, approved like any email.
  - **Booking a call:** open 30-minute times come from your Google Calendar (weekdays 9–5, next 7 days). Pick one to book (invite plus Meet link; the prospect moves to Discovery), or up to three to email as options. Each booking is an `ae.book_meeting` approval; it's marked booked only with Calendar's event id. The AE can write a discovery call brief.
  - **Demos, only for companies that said yes:** "yes" means a reply read as interested or wanting a call, or a booked call. The engine refuses both the demo design (`architect.demo_spec`, level 2, you review it) and the build (`architect.build_demo`, approval) otherwise. The build uses the design you accepted, not what the page sends. The agent is created in your ElevenLabs account tagged `demigod-demo`, with its share link saved on the prospect and an "Email the demo link" draft.
  - **Agreements:** pick your DocuSign template, signer role and optional fee field labels in Settings. Each send is an `ae.send_agreement` approval with the exact fees; it's marked sent only with DocuSign's envelope id. **Check signature** reads the real status; once completed, one click makes them a client with the signed fees and opens PM Suite project setup.
  - **Finding prospects:** CSV import (columns matched by name, duplicates by company or email skipped). **Find prospects (web)** has the SDR run up to 5 live web searches and return companies with their source URLs, for your review before any are added. **Research online** on a prospect does up to 3 searches. Web searches cost $0.01 each, are counted in the daily AI cap, and are logged per task.
  - **Client ROI:** put the client's live ElevenLabs agent id on the client, then **Pull this month's calls** (total and rated successful). The numbers are saved on the client and given to the Account Manager's monthly report, labeled as coming from the ElevenLabs call log.
  - **Not verified with real accounts yet:** what Calendar `create_event`, DocuSign `createEnvelope`/`getEnvelope` and ElevenLabs `agents_create`/`agents_get_link` return. The app looks for the id in the usual places and refuses to mark anything done without one. Live web search must be enabled for your Anthropic organization.
  - **Tests:** engine `test_hq` has 28 Phase 10 checks; app `test/disales.js` covers the whole loop with mocked connectors.
- [x] Phase 11: outreach campaigns and talking to the AI Sidekicks agents (app `06g-dicampaign.js`, engine `hq.py`).
  - **Ask the SDR:** in HQ, open the SDR and say what you want ("reach out to 100 property managers in Orlando"). The SDR answers from your real records and offers a filled-in campaign form. Nothing starts until you press Start drafting. The other AI Sidekicks agents talk with the right persona now and point you to the record to open. Only the SDR sets up campaigns.
  - **A campaign:** pick prospects you haven't emailed and/or have the SDR find new companies on the web (up to 100, in chunks of up to 25, max 5 searches each). Only companies with a public email are added; ones already emailed or on do-not-email are skipped. The SDR drafts a first-touch email for each (intro emails, not proposals: proposals come after a discovery call).
  - **One review, one approval:** every draft is on one screen to edit or drop. **Approve** is bound by the engine to exactly the emails shown (a hash); if anything changed, it refuses. Each kept email becomes its own approved `sdr.send_email` task with your footer (name and mailing address, required by CAN-SPAM) and an opt-out line.
  - **Paced sending:** the engine decides what's due: weekdays 9am–5pm (configurable as `outreach_window` in `config.yaml`), under the campaign's daily limit (default 20, max 50, counting all outreach sent that day). The app sends one at a time, 2.5–6 minutes apart, from your Gmail, only while JARVIS is open with the PC link on, and only for the owner. Each send is claimed first so two open windows can't send it twice; an unconfirmed send goes back in line after 10 minutes, up to 3 tries.
  - **Control:** pause, resume or cancel any time. Opting out (reply or Do not email) cancels that person's pending email. Sends are logged on the prospect, so follow-ups and reply checks work as before.
  - **Tests:** engine `test_hq` has 22 campaign checks; app `test/dicampaign.js` covers the flow end to end with mocked Gmail.
- [x] Phase 12: AI Sidekicks by voice ("Hey Jarvis") and in the app's JARVIS chat (voice `jarvis_voice.py`, engine `hq.py`, app `06g-dicampaign.js`, `16-assistant.js`).
  - **Say it:** "Hey Jarvis, tell the SDR to find 100 property management companies in Massachusetts." The engine starts a prospect search: up to 100, in parts of 25 that run one after another, each told what the earlier parts already found so they don't repeat. Up to 5 web searches per part ($0.05). Nobody is contacted.
  - **Then:** "Have the SDR draft intro emails for them." When the search is finished, the companies with a public email become a campaign in drafting. Saying it accepts the search results (recorded on the tasks). It refuses while the search is still running, if your footer isn't set, or if emails for that search already started.
  - **Never by voice:** approving or sending. You review and approve the emails in the app (Prospects → the campaign). Voice can also report status ("how's outreach going", "any replies?") and pause a sending campaign.
  - **App side:** the footer you set in Settings is copied to the engine. Companies a voice-started campaign found are added to AI Sidekicks' prospects automatically, with their source. Prospects shows **Prospect searches** with progress and a **Draft intro emails** button. The app's typed JARVIS has the same tools in AI Sidekicks.
  - **Tests:** engine `test_hq` has 14 search checks and `tests/test_voice_di.py` 11 voice checks; app `test/disearch.js` has 11.
  - **Email lookup:** most companies don't publish an email on directory pages (first real run: 89 found, 17 with an email). "Find emails" (voice, app or JARVIS chat) queues one `sdr.find_contact` task per company without one: up to 3 web searches, company site first, then official listings. An address counts only if it comes with the URL of the page that shows it; the agent is told never to guess. About 4–5 cents per company. Emails found after drafting started join the same unapproved batch.
  - **Cut-off replies (fixed after the first real run):** the web-search agent had 3,000 output tokens, too few for 25 companies, so replies were cut off and blocked as invalid JSON. Now 8,000, with short notes; the answer is read only from the text after the last search result; a cut-off list keeps its complete entries; otherwise one repair turn without web search rewrites it as JSON (its cost is logged). `tests/test_ai_json.py`.
- [x] Campaign emails without placeholders (`agents_ai.py` `sdr.campaign_email`, `hq.py`, app `06g-dicampaign.js`).
  - **Found in the first real batch:** the general outreach writer didn't know who was sending or what AI Sidekicks sells, so drafts were full of "[First name]", "[Your name]" and "[One sentence on what AI Sidekicks does]"; 6 of 17 failed outright (cut off).
  - **Now:** campaign emails use a dedicated writer that's given the sender (from your footer) and the PM Suite facts (AI receptionist, maintenance intake, leasing and showings, dashboard), told to greet "Hi there" or the team by name when there's no contact, sign with your name, and never leave brackets or blanks. Any email (campaign or single) with a placeholder is refused by the engine; campaign drafts with one are flagged "Has a placeholder", block approval, and can be edited or redrafted (**Redraft N**). Drafts written before the fix are re-checked and flagged. `test_hq` Phase 13 (8 checks).
- [x] AI Sidekicks' pitch is yours to set (`agents_ai.py`, `hq.py`, app Settings → What AI Sidekicks offers). The default now says the team builds custom AI assistants and automations around each company's workflow (calls, texts and email; maintenance; leasing and showings; owner reporting; back-office work), and campaign emails pick at most two examples that fit the company instead of pitching only a voice receptionist. **Redraft all** rewrites a whole unapproved batch from the current pitch (it replaces your edits, and asks first). `test_hq` Phase 14, app `test/draftview.js`.
- [x] CRM plans by the Solutions Architect (only for companies that said yes, or client projects; you review the plan), and intro emails mention custom CRMs (Phase 17; `agents_ai.py` `architect.crm_plan`, app `06f-disales.js`, `07j-agentos.js`; `tests/test_crm_plan.py`).
- [x] Morning brief across both businesses (Phase 16; `hq.morning_brief`, voice `morning_brief`, app `05e-brief.js`; `tests/test_morning_brief.py`).
- [x] AI Sidekicks email identity, campaign results, and engine email (Phase 15; `hq.py`, `gmail.py`, `agents_ai.py`, `run.py`, app `02d-dimailbrand.js`, `06g-dicampaign.js`). Follows the owner's "Professional email & outreach system" brief.
  - **Writer:** campaign emails follow the brief: a natural greeting, AI Sidekicks as a company that builds custom AI assistants that work alongside a team and give it time back, one true observation about the company, at most two practical examples, the business value (time back, faster responses, less repetitive work), a low-pressure 15-minute ask, an opt-out line; 80–130 words; never invents details; no sign-off (the signature is added). The default pitch is rewritten from the brief and stays editable in Settings.
  - **Signature:** one block on every AI Sidekicks email, attached by the engine when the email is created (so it's part of what you approve): name, AI Sidekicks, "AI Assistants That Answer Every Call, Text & Follow-Up", and the mailing address (required for commercial email). It replaces the old separate footer line. HTML version with the official logo (the app's lockup, resized, embedded inline with alt text) plus a plain-text fallback; any sign-off the writer adds is removed so there's never a duplicate. Phone, website and links are only added if you provide them.
  - **Results:** each campaign shows sent, replied (and rate), interested, want a call, calls booked, opted out; the campaign list and "Hey Jarvis" status include them.
  - **Engine email:** after a one-time Google sign-in (`python run.py gmail-auth`, your own OAuth desktop client in `data/google_client.json`), JARVIS Engine sends approved campaign emails itself (same window, daily limit, claim and pacing) and reads replies every 10 minutes, with Claude closed. The app stops sending when the engine is active. Settings → Engine email shows status and turns it on or off. Standard library only; the refresh token stays in `data/gmail_token.json` on the PC.
  - **Send me a test:** in a campaign. With engine email connected it goes to the signed-in Gmail address only; otherwise the app asks for your address and confirms first. It doesn't approve or queue anything.
  - **Tests:** `tests/test_engine_email.py` (24 checks, Gmail faked); app `test/dicampaign.js` checks the signature, inline logo, results and test send.
- [x] PC link drops (`db.py`, `hq.py`, `run.py`, app `17-pclink.js`).
  - **Causes found:** (1) "run now" ran the agents' AI work (web searches take 20–60 s each) inside the PC-link server that Claude runs, so every other call from the app waited and timed out; (2) the engine loop and the link server write the same SQLite file and could hit "database is locked"; (3) the app turned the link off for good on a single "not connected" (for example when Claude restarts its local server or the PC wakes from sleep) and only a page reload brought it back.
  - **Fixes:** run now hands work to the engine loop, which wakes within 2 seconds (it only runs inline when the loop isn't running); the database uses WAL and waits up to 30 s for the other process; the app rides through one or two failed calls, shows the link as off only after it keeps failing, and keeps retrying (10 s, 20 s … up to 2 min) so it reconnects by itself. App `test/pclink.js` (7 checks), engine `test_hq` (3 checks).
  - **Still true:** the link server is started by the Claude desktop app, not by Start-JARVIS. After an engine update, fully quit and reopen Claude so it runs the new code; the link is down while Claude is closed or the PC is asleep.
- [x] Voice speed (`jarvis_voice.py`). Before: you stop talking → 1.4 s silence wait → transcribe → the "On it, boss" was synthesized and fully played before Claude even started → Claude's first sentence was held until the second began → each sentence downloaded as a full MP3, then played, then the next was requested.
  - **Now:** 0.9 s silence wait (`end_silence`); the acknowledgment is pre-made at startup and plays while Claude is already working; a first sentence that isn't a "let me check" filler is spoken the moment it's complete; ElevenLabs streams raw audio that plays as it arrives (falls back to the old request, then Edge, then Windows); the next sentence is synthesized while the current one plays; system prompt and tools are prompt-cached; optional `voice.model` for a faster model. Each reply prints its timing.
  - **Tests:** `tests/test_voice_speed.py` (10 checks, audio and network faked). Not measured on your PC yet.

## 8. Architecture review (after Phase 4)
- **What held up:** the ops layer stayed the single write path, so the new AI Sidekicks records, agent drafts and approvals all inherited validation, confirmation, idempotency and per-business audit with no new plumbing.
  - The engine's durable queue, leases and hash-bound approvals carried the new AI executors unchanged.
  - Splitting the read and write channels made owner-only actions (the cap, approval, review) enforceable on the backend.
- **Where data lives, and why:**
  - AI Sidekicks records stay in the app's account storage, as in Phase 1.
  - Agents work from a snapshot of the record sent with the task, so the engine never needs a second copy of the agency's database.
  - Results come back as drafts, and the app is still the only writer to AI Sidekicks records, after checking the engine's review state.
- **Known limits, stated plainly:**
  - **Live research:** Research and SDR work from model knowledge and your records. They don't do live web research, because the search tool costs extra and wasn't approved.
  - **Real API calls:** the first real AI calls happen on your PC with your key. The engine tests here use a fake client, so a real call hasn't been verified.
  - **Background work:** agents run only while the PC's worker loop runs. The cloud host is still pending.
  - **Ticket intake:** tickets are entered by you. Intake from a client's email or phone is a later step, and would need connectors and your approval.
  - **VR:** VR still covers the MolaBucks floor only.
