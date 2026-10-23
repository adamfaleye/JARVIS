# MolaBucks Capital HQ in VR (Meta Quest 2)

HQ runs on the Quest in **Meta Quest Browser** through WebXR. It's the same app, the same 3D office and the same engine operations as your desktop. The headset is a screen. The agents keep working in JARVIS Engine on your PC whether the headset is on, off or disconnected.

> **Status:** I built this and tested it on a desktop with Meta's WebXR emulator (IWER), set to a Quest 2. **It has not been tested on a physical Quest 2.** Until you try it on the headset, treat VR as unverified. That includes the frame rate.

## How it fits together

```
Quest 2 (Meta Quest Browser) ──HTTPS on your Wi-Fi──► python run.py web   (this PC)
   JARVIS app + WebXR                                    │ same crm_pull / crm_push functions as the PC link
                                                         ▼
                                              JARVIS Engine: hq.py, SQLite, worker loop (run.py loop)
```

- **Why a server on the PC:**
  - WebXR only works on secure (HTTPS) pages.
  - The headset can't use the Claude desktop app's PC link.
  - So `run.py web` serves the JARVIS page and the two sync tools the app already uses.
  - There are no new business rules. Approvals still go through the engine's hash-bound approval check.
- **Rendering:**
  - The 3D office you already have (Three.js r128) gets WebXR switched on. It isn't a second engine.
  - For the headset, HQ rebuilds the office in a "VR" quality: no post effects, no shadows, 3 lights instead of 8, and static geometry merged.
  - Faraway agents switch to a single merged copy of themselves.
  - In the emulator, the lobby view went from 528 draw calls to 135. Desktop quality is untouched.

## Set up (once)

1. On the PC, double-click **Setup.bat** again. It adds the `cryptography` package, which makes the HTTPS certificate.
2. Put the headset on the same Wi-Fi as the PC.

## Use it

1. **Start the agents.** On the PC, run **Start-JARVIS** as usual so the agents keep working.
2. **Start the VR server.** Double-click **Start-HQ-VR**. A window shows:
   - an address, such as `https://192.168.1.23:8443`
   - a **6-digit pairing code** (valid 15 minutes, single use)

   The first time, Windows asks whether Python may use the network. Allow **Private networks**.
3. **Open HQ on the Quest.**
   1. Open **Meta Quest Browser** and type that address. Bookmark it.
   2. The browser warns about the certificate once, because this PC made it. Choose **Advanced → Proceed**.
4. **Pair the headset.** JARVIS asks for the pairing code. Type it in. The headset is now paired; you won't need a code next time unless you unpair it.
5. **Enter VR.** Go to **HQ** and press **Enter VR**. It's in the HQ toolbar, in the HUD (the **VR** button), and on the welcome screen.
6. **Leave VR.** Use the menu's **Exit VR** button or the Quest's own button. You're back in the normal page.

**Commands** (from the JARVIS Engine folder):

| Command | What it does |
|---|---|
| `python run.py web --pair` | Prints a new pairing code. The running server accepts it. |
| `python run.py web --revoke` | Unpairs every device. |
| `python run.py web --local` | Serves only to this PC, for the USB fallback below. |

**If the headset can't reach the PC:**
- Your network may isolate devices, as many guest networks do, or the firewall may be blocking Python.
- Fallback: connect the Quest by USB with developer mode on.
  1. In Chrome on the PC, open `chrome://inspect/#devices` → **Port forwarding**.
  2. Forward `8443` to `localhost:8443`.
  3. Run `python run.py web --local`.
  4. On the Quest, open `https://localhost:8443`.

## Controls (Touch controllers)

| Control | Action |
|---|---|
| Left stick | Walk, relative to where you look. Constant speed, and walls stop you. |
| Right stick left/right | Snap turn 30° (default). Smooth turning and 45° are under **Comfort**. |
| Right stick up/down while pointing at a panel | Next or previous page |
| Trigger | Select whatever the ray points at: a panel button, an agent, or a station |
| A or X | Open or close the menu |
| B or Y | Back |
| Menu → Return to lobby | If you ever get stuck |
| Menu → Go to… | Short fade, then you're at a station |

- **Prompts:** pointing at an agent or a station shows a prompt such as "TRIGGER · Talk to Hunter".
- **Left wrist:** shows the time, LIVE/STALE/OFFLINE, approvals waiting, and the frame rate if you turn it on.
- **Panels:**
  - Panels open next to the thing you selected and stay put; they don't follow your head.
  - **Bring here** moves a panel in front of you, and **✕** closes it.
- **Hand tracking:** pinching should select (the browser maps it to "select"), but walking needs the controllers. This is untested.

## What works in VR (same operations as desktop)

| Area | In VR |
|---|---|
| JARVIS | Goal, realized vs. projected, plan, latest briefing, "Run today's briefing", "Blocked & failed work", pause/resume automation (with confirmation). Setting a new goal needs typing, so it stays on desktop. |
| Agents | Real state, current task, 7-day scorecard, task history with errors. Assign work (any non-level-3 task type, with confirmation). Conversation history is read-only; typing stays on desktop. |
| Task detail | Status, timings, attempts, verification, summary, error, fix, and event log. Retry, Cancel and Review & approve. |
| Approval station | Shows the waiting action with **exactly** the parameters being approved. **Approve exactly this** asks for confirmation, then calls the same `hq_action approve` with the params hash, so any later change sends it back to you. Reject is available too. Offer approvals stay on desktop. |
| Task queue | Running, needs approval, queued, failed/blocked, done. "Run due tasks now" (the engine refuses it while paused). |
| Pipeline | Stages and deals from your records. A deal shows its underwriting: asking, price, ARV, repairs, MAO and fee. Missing numbers say "not entered"; nothing is filled in. It also shows the MAO rule as your stated assumption, plus activity and calls. You can move one stage forward or back through the same `move_stage` operation; its prerequisite warnings appear as a confirmation. Closing stays on desktop because it needs the fee you collected. |
| Live feed | Real engine events. Select one to open its task. |

**Rules that carry over unchanged:**
- Every confirmation that pops up on desktop pops up on the headset panel. It's the same `confirmSheet` step, routed to VR.
- Entering VR grants nothing extra. Leaving VR cancels nothing.

## Data honesty

- **Sync state:** the panel header and your wrist show one of three states:
  - **LIVE** (synced within 25 s)
  - **STALE** (reconnecting, with the age of the data)
  - **OFFLINE** (engine not connected)
- **Network drops:** in testing, killing the server mid-session left VR running. The panels showed OFFLINE, and they came back to LIVE when the server returned, without pairing again.

## Security

- **Device tokens:**
  - The headset gets a device token only by entering the code shown on your PC: 6 digits, 15 minutes, 5 tries, single use.
  - Only a hash of each token is stored, in `data/jarvis.db` (kv `web_devices`).
- **What's exposed:**
  - Only `crm_pull`, `crm_push` and `list_counties` are available over the network.
  - The read tool still refuses writes.
  - API keys never leave the PC.
- **Access log:** writes go to `data/web/web.log`, without tokens.
- **Certificate:** self-signed for this PC's addresses, in `data/web/`. It's re-made automatically if the PC's address changes; the browser then warns once more.

## Testing

**Physical Quest 2: not yet done.** When you test, please check:
1. Does the page load, and does the certificate warning let you proceed?
2. Does pairing work?
3. Does **Enter VR** start a session? Are the floor height and the panel positions right?
4. Do the sticks, snap turn, trigger, A/X and B/Y work?
5. Can you open Hunter and see real tasks?
6. Can you approve something in the approval station?
7. Does **Exit VR** bring you back to a working page?
8. What frame rate does the wrist show? Turn it on under Comfort.

Verified in the desktop emulator (IWER 2.6, Quest 2 profile, Chromium with software rendering) against a real JARVIS Engine web server:

- Pairing screen; wrong code refused; right code pairs.
- HQ loads; **Enter VR** appears only when immersive VR is supported.
- VR session starts; the headset build is used (683 meshes merged into 146).
- Menu opens; desktop page shows "You're in VR" with an Exit button.
- Left stick walks; snap turn is exactly 30° and pivots on the head; collision keeps you inside.
- A opens the menu; the ray highlights the button it points at; trigger selects; B goes back.
- Return to lobby / Go to teleport with a fade.
- Agents → Hunter shows real engine tasks.
- Approval: review screen, then confirmation. Nothing is approved before you confirm. The engine records the approval with its hash, and it appears in the activity log.
- Cancel in a confirmation leaves the task alone.
- Pipeline: a synced deal moved New Lead → Contacted through `move_stage`.
- Pointing at an agent shows "Talk to …"; trigger opens its panel next to it.
- Engine killed mid-session: OFFLINE shown, session survives; engine back: LIVE again.
- Exit VR ends the session, desktop HQ comes back at desktop quality, and no background task changed.

## Known limitations and next steps

- Unverified on a physical Quest 2, including comfort, text size and frame rate.
- **Frame rate:**
  - The target is 72 fps, but it hasn't been measured on the device.
  - If it's low, next steps are: lower the render resolution (`setFramebufferScaleFactor`), merge more materials, and cap the office's screen-texture updates.
- **Text entry:** there's no keyboard input in VR. Goal setting, typed chat, offer approvals and closing a deal stay on desktop.
- **Quest Browser features:** no dom-overlay and no controller 3D models. The controllers are drawn as simple shapes, so there's no download from Meta's model CDN.
- **Hand tracking:** select only; untested.
- **Always-on:** the PC must be on for both the agents and the VR server. An always-on host is still the open deployment decision (see ARCHITECTURE.md).
