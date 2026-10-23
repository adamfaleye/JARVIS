# MolaBucks Capital HQ

HQ is a 3D office you walk around, sitting on top of a real task system. JARVIS is the CEO. Six AI employees each have one job:

| Agent | Job | Real work it does today |
|---|---|---|
| **Hunter** | Acquisitions intelligence | Syncs county sources, ranks the strongest scored leads, reports the review and skip-trace backlog |
| **Closer** | Dispositions & sales | Reviews contracted deals for blockers: no buyer match, no fee, no closing date, upcoming closings |
| **Dispositions** | Buyer matching | Matches contracted deals to your buyers' markets and max prices, and flags weak demand |
| **Follow-up** | Pipeline persistence | Lists who's due today and what's overdue, checks campaign and reply health, launches a campaign only with your approval |
| **Front Desk** | Communications intake | Finds conversations waiting on a reply, reply drafts ready to send, hot leads, opt-outs |
| **Finance** | Finance & operations | Realized vs. projected fees, EMD and closing deadlines from your contracts, Twilio balance (only when Twilio reports it) |

## How it runs
- Every task is a row in `data/jarvis.db` (`hq_tasks`). Each one has an id, owner, objective, status, attempts, timestamps, an event log (`hq_events`) and a result.
- The engine loop (Start-JARVIS) runs the queue every 30 seconds:
  - An agent works on one task at a time.
  - Workers lease a task, so two processes never run the same one.
  - If the PC restarts mid-task, the task is picked up again.
  - Failures retry with backoff (1, 2, 4 minutes), then stop with a "how to fix" note.
  - A task whose dependency failed is marked **blocked**, not silently skipped.
- **A task only counts as done when its result checks out.** Otherwise it's marked blocked with the reason.
- **Schedule:** a morning briefing at 8 AM and an end-of-day review at 6 PM, on weekdays. Change the times under `hq:` in `config.yaml`.
- **Goal → plan:** tell JARVIS the monthly target. It starts from your *collected* fees, works backward (closings → contracts → offers → conversations) using your own conversion history when there's enough of it (otherwise stated starting assumptions), and assigns today's work. It's a plan, not a promise.

## Approval levels
1. **Internal work** (reading records, scoring, matching, summaries) runs on its own.
2. **Drafts** (messages, recommendations) are prepared for you, not sent.
3. **Outside commitments** (launching a campaign, spending) wait in the **Approval station**.
   - Approving covers the exact parameters you reviewed.
   - If anything changes afterward, the task goes back to the tray.
   - No agent can approve, change policies or change its own permissions.

## In the app
- **HQ** is the first item in the JARVIS nav. Press **Enter immersive** (or F) for the full-window game view; Esc leaves it.
- **Graphics** (gear icon): Auto, Ultra (native or sharper, floor reflections, crisp shadows), High (renders at 1080p with HDR bloom, MSAA and soft shadows), Balanced (720p), Performance. Auto steps down by itself if the frame rate drops.
- **Controls:**
  - Computer: click to look, WASD to move, Shift to run, E to talk or open, Tab for the map and fast travel, 1–4 to jump to an objective, M for sound, F for immersive.
  - Phone: left thumb stick to move, drag to look, tap to open.
- **HUD, all from live data:**
  - Compass with markers for every agent (colored by real state), JARVIS, approvals and the meeting room.
  - Minimap.
  - Objectives built from what actually needs you: approvals, no goal set, failed or blocked tasks, an unread report, and the work loop not running.
  - Live event feed: an agent started a task, finished one, or failed; something new needs approval; a new report arrived.
  - Sync status showing seconds since the last engine update.
- **The world follows real state:**
  - Agents type only while a task is running.
  - An agent waiting on your approval raises a hand under an amber marker.
  - A failed or blocked agent slumps with red eyes under a red marker.
  - Idle agents look at you when you walk up.
  - The approval station's beacon lights only when something is waiting.
  - JARVIS pulses faster while any agent is working.
  - The skyline follows your local time (night, sunrise, day, sunset).
- **Operations** mode is the same thing as a plain list. It's used automatically when a device has no 3D support.
- **Talking to an agent:** the agent answers only from its real state. Work it agrees to becomes a task you can watch; it never claims work is done before the task finishes.
- **Logo:** walk up to the lobby wall and press E (or use Go to → Logo wall) to upload your logo. "MOLABUCKS CAPITAL HQ" sits under it.
- **Voice:** "Hey Jarvis, what's the team doing?" uses the `hq_status` tool. Approvals happen in the app, not by voice.

## Tests
`python -m tests.test_hq` covers:
- the task lifecycle, dedupe, leases and recovery
- retries, blockers and approvals (including approvals voided by edits)
- goal → plan → assignments
- the schedule, briefings, status truthfulness and the PC-link bridge

## What's next (checklist)
- [ ] Hunter → Closer handoff tasks created automatically when a lead is promoted
- [ ] Front Desk drafts replies as level-2 tasks (today the Inbox texting agent drafts them)
- [ ] Follow-up proposes drips for due leads as approval-tray items
- [ ] Agent scorecards over 30/90 days, with lead-to-contract attribution
- [ ] Distinct voices per agent (ElevenLabs) in conversations
- [ ] Integration health panel (Twilio, AvaData, Anthropic) on the command wall
