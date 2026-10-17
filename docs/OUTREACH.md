# Outreach: campaigns, drips, numbers, dialer

Everything sends and calls through your own Twilio account. You run it from JARVIS: **Campaigns**, **Templates**, **Call Log**, **Numbers**, **Do Not Contact** and **Realtors**, plus the **Dialer** button. The PC engine has to be running (Start-JARVIS).

## Setup
1. Fill in `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM` in `.env`. If you have an A2P messaging service, add `TWILIO_MESSAGING_SERVICE_SID` too.
2. Get A2P 10DLC approved in Twilio, then set `sms: enabled: true` in `config.yaml`.
3. Restart: Stop-JARVIS, Start-JARVIS, and fully quit and reopen Claude.
4. In JARVIS → **Numbers**, set your cell (for the dialer) and a default sending number.

## Campaigns
- **Blast:** one message, sent once.
- **Drip:** several messages on day offsets (day 0, 2, 6…).

The first message must include opt-out wording ("Reply STOP to opt out"). Merge fields: `{first}` `{street}` `{city}` `{sender}` `{company}`.

Launching asks you first and shows who will and won't be texted, and why. Who gets texted:
- people with recorded permission: a deal's outreach permission is "Granted", a realtor or buyer marked as permitted, or a list where you confirmed permission
- people who texted you first
- Hunter contacts approved for automated outreach

A skip-traced number alone is never enough.

Always enforced:
- texting hours (`quiet_hours`)
- the daily limit (`daily_limit`) and a per-number daily limit (set in Numbers)
- at most one text per person every 20 hours
- Do Not Contact and STOP
- opted-out or archived deals
- the Emergency stop

When someone replies, every drip they're on stops for them, and the reply lands in the Inbox.

The old `config.yaml` follow-up sequence now shows up as a drip campaign called "Follow-up sequence."

## Numbers
- JARVIS reads the numbers on your Twilio account.
- Each person stays on the number they first heard from.
- Health shows sent, delivered, failed, carrier-filtered, replies and opt-outs over 7 days.
- You can search for and buy numbers (with confirmation). A new number needs to be in your A2P messaging service before it texts.

## Dialer and call log
The Dialer rings your cell first, then connects the seller. Caller ID is your Twilio number.
- Calls are only placed during calling hours (default 9 AM to 8 PM; never outside 8 AM to 9 PM).
- It never dials Do Not Contact.
- Hunter numbers need "OK to call manually" first.
- Calls aren't recorded.

The call log shows dialer calls, other outbound calls and inbound calls (missed ones flagged) from the last 30 days.

To forward calls that come in to your Twilio number:
1. In Twilio, create a TwiML Bin with `<Response><Dial>+1YOURCELL</Dial></Response>`.
2. Set it as the number's "A call comes in" handler.

## Do Not Contact
STOP replies are added automatically and can't be removed (the person has to text START). Numbers you add by hand can be removed.

## Tests
`python -m tests.test_outreach` runs the checks against a fake Twilio client and a throwaway database. Nothing is sent.
