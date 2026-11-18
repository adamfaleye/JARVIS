# AI Sidekicks Instagram assistant (@aisidekicks)

The Instagram DM assistant for AI Sidekicks' own account, and the AI Sidekicks case study. It answers questions about AI Sidekicks' services (custom AI assistants, custom CRMs, business automation) and turns interested people into **discovery-call requests that Mola approves**. A draft is never a booking.

Code: `engine/igassist/`. Data: `data/clients/aisidekicks.db` (git-ignored; never push it, the repo is public).

---

## 1. How it works

```
Instagram DM ─► /webhooks/instagram (signature checked, stored, 200 OK)
            ─► worker: dedupe ─► conversation engine (rules + Claude, verified facts only)
                 ├─ escalation? → Mola takes it; AI pauses in that conversation; holding reply
                 ├─ question?   → answer from the knowledge base (or "let me check with Mola")
                 └─ wants a call → collect name, need, preferred day/time → CALL REQUEST DRAFT → Mola's queue
            ─► outbox: guards → draft for Mola (review mode) or send (auto mode, routine only)
Mola: check calendar → mark the slot verified → approve → put it on the calendar → confirm → confirmation to the customer
```

**Status model:** New inquiry → Qualifying → Draft created → Awaiting availability → Ready for Mola → Mola reviewing → Approved → Awaiting calendar entry → Entered in calendar → Confirmed. Side exits: Information needed, Declined, Cancelled, Completed.

**Enforced in the backend (`workflow.py`, `outbox.py`), not just the UI**
- Only the owner role (Mola) can verify availability, approve, record the calendar entry, confirm, decline or cancel. An operator account gets a 403 on those.
- Approval needs the exact slot verified and every required detail. Changing the date or time resets the check; editing a booking detail after approval voids it (hash-bound).
- The customer can be told "booked" only once the request is Confirmed: a recorded calendar entry plus Mola attesting it's on his calendar. A typed "you're booked" in a conversation with nothing confirmed is blocked too.
- AI text guards: no prices (pricing is scoped on the call), no discounts, guarantees, durations, policies or availability claims Mola hasn't provided, no "you're booked", no placeholders, 1,000-byte Instagram limit, at most one emoji. If the model's reply fails a guard, a template built from verified facts goes instead.
- Escalation rules always win over the model: exact-price demands, guarantees, refunds, complaints, legal or sensitive topics, existing-client questions, and anything on Mola's "always personal" list. The AI stays quiet in an escalated conversation until Mola hands it back.
- Takeover, return to AI and a global pause. Mola replying from the Instagram app takes the AI's hands off that conversation.
- No sends outside the 24-hour window, retry with backoff, nothing counts as sent without Instagram's message id, duplicate webhooks ignored, deleted messages erased.
- **Test mode:** nothing reaches Instagram until Mola approves the pilot (Settings) and turns sending on. Simulator conversations run the same pipeline and are never sent.

---

## 2. Run it

- **Start:** double-click `Start-Sidekicks.bat`. The first time, a browser page opens to create your login (the link works for 1 hour). It runs with no window; the log is `data\clients\aisidekicks-server.log`.
- **Stop:** `Stop-Sidekicks.bat`.
- Dashboard: http://127.0.0.1:8600 on this PC, or your Tailscale Funnel address (`https://<your-machine>.<tailnet>.ts.net`).
- Command line (same thing): `python run.py assistant serve`, `assistant status`, `assistant backup`, `assistant meta-check`, `assistant meta-subscribe`. The default client is `aisidekicks`.
- Tests: `python -m tests.test_igassist`.

---

## 3. Meta (Instagram API with Facebook / Unified Login)

Host `graph.facebook.com/v26.0`, `POST /<IG_ACCOUNT_ID>/messages`, permissions `instagram_basic` and `instagram_manage_messages`, a business system-user token (doesn't expire). Webhook signatures are checked with the Meta app secret.

**Your remaining steps**
1. In the Meta app, add **@aisidekicks** as an Instagram tester and accept the invite in Instagram.
2. Generate the token for @aisidekicks.
3. Add these four lines to `.env` yourself (never paste them in chat):
   ```
   AISIDEKICKS_IG_APP_SECRET=
   AISIDEKICKS_IG_VERIFY_TOKEN=   (Set-Instagram-Keys fills this in with a random value)
   AISIDEKICKS_IG_ACCESS_TOKEN=
   AISIDEKICKS_IG_ACCOUNT_ID=
   ```
4. Run `Start-Sidekicks.bat`.
5. Meta app → Webhooks: callback `https://<your-machine>.<tailnet>.ts.net/webhooks/instagram`, the `AISIDEKICKS_IG_VERIFY_TOKEN` value from your `.env`, subscribe to `messages`.
6. DM @aisidekicks from another account and watch the Inbox.

Standard Access covers accounts you manage (your own). DMs from the general public will likely need App Review and Business Verification for AI Sidekicks LLC.

---

## 4. Security and privacy

- Passwords are scrypt hashes; session tokens are stored only as hashes; 12-hour HttpOnly, SameSite=Strict cookies (Secure behind HTTPS); CSRF header on every write; lockout after 5 wrong passwords; per-response CSP nonce and frame-deny.
- Secrets live only in `.env`. Model calls send verified facts plus recent messages inside a block marked as data, never as instructions.
- Separate SQLite file; back it up with `python run.py assistant backup`. Optional retention in Settings. Per-customer export and delete on the Customers screen.
- Encryption in transit by HTTPS on the public URL; at rest, use BitLocker on the PC.
- `data/clients/` is git-ignored. Never push it.

---

## 5. Knowledge base

Edit it in the dashboard (Knowledge Base). It starts with only what Mola has stated: the four services (custom AI assistant, custom CRM, business automation, not sure yet), "pricing is scoped on a discovery call", and the escalation rules. Anything still missing is on the setup checklist, and the assistant hands those questions to Mola instead of guessing.
