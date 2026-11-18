"""Conversation engine. For each new customer message:

1. Rules (extract.py) read intents, escalation triggers and booking details. These always run.
2. If the model is available, it writes a natural reply from verified facts only, and may catch details the rules
   missed. It can add an escalation but can't remove one, and its reply goes through the same guards as any AI text.
3. Booking requests become (or update) a draft in the owner's queue (Mola, for AI Sidekicks). The draft is never a booking.
4. The reply is queued: as a draft for the owner in review mode, or to send in auto mode for routine topics only.
All brand wording comes from the client's profile (kb.profile), so the same engine serves every client.
If the model is unavailable, over the daily AI cap, or its reply fails a guard, a plain template reply is used.
"""
import json
import os
import re

from . import extract, kb, outbox, store, workflow

AI_ENABLED = None      # None: on when ANTHROPIC_API_KEY is set. Tests set True/False.
AI_CONNECT = None      # returns the JARVIS engine connection used only for AI spend logging and the daily cap
HISTORY = 12

HOLDING = {
    "complaint": "I'm really sorry to hear that. I'm passing this to {owner} now so {she} can reply to you personally.",
    "refund": "That's something {owner} handles personally. I've let {him} know and {she}'ll get back to you directly.",
    "discount": "Pricing questions like that go to {owner} personally. I've let {him} know and {she}'ll get back to you.",
    "cancellation": "Thanks for letting us know. I've passed this to {owner}, and {she}'ll take care of it with you directly.",
    "reschedule": "Thanks for letting us know. I've passed this to {owner}, and {she}'ll confirm any change with you directly.",
    "existing_appointment": "Thanks! {owner} keeps track of {his} own calendar, so I've passed this to {him} and {she}'ll get back to you.",
    "sensitive": "Thanks for telling us. {owner} will want to answer this one personally, so I've passed it to {him}.",
    "uncertain": "Good question. {owner} will want to answer this one personally, so I've passed it to {him} and {she}'ll get back to you.",
    "always_personal": "Thanks for reaching out! {owner} handles this personally, so I've passed it to {him}.",
    "unknown": "Good question! Let me check with {owner} and get back to you.",
    "model": "Thanks! I've passed this to {owner} so {she} can reply to you personally.",
}
ESCALATION_LABELS = {"complaint": "Complaint", "refund": "Refund request", "discount": "Discount or pricing exception",
                     "cancellation": "Cancellation", "reschedule": "Reschedule request", "existing_appointment": "Question about an existing booking",
                     "sensitive": "Sensitive topic", "uncertain": "Needs the owner's judgment", "always_personal": "Topic the owner handles personally",
                     "unknown": "Question the knowledge base doesn't cover", "model": "AI flagged for the owner"}


def fmt(con, text):
    return kb.fmt(con, text)


ESCALATION_ORDER = ["complaint", "refund", "sensitive", "cancellation", "reschedule", "existing_appointment", "discount", "always_personal", "uncertain"]


def ai_available():
    if AI_ENABLED is not None:
        return AI_ENABLED
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _ai_con():
    if AI_CONNECT:
        return AI_CONNECT()
    from .. import hq
    return hq.connect()


# ------------------------------------------------------------------ templates (verified facts only)
def _owner(con, possessive=False):
    first = store.get(con, "voice_person") == "first_person"
    p = kb.profile(con)
    return ("my" if possessive else "I") if first else (p["owner_possessive"] if possessive else p["owner"])


def service_line(con, key):
    s = kb.services(con).get(key)
    if not s:
        return ""
    p = kb.profile(con)
    if s.get("assistant_text"):
        return s["assistant_text"]
    if s.get("consultation"):
        pitch = s.get("pitch") or f"We build {kb.soft_lower(s.get('plural') or s['label'])} around how your business works."
        return fmt(con, f"{pitch} {p.get('pricing_note') or ''}").replace("  ", " ").strip()
    line = f"The {kb.soft_lower(s['label'])} is {kb.price_text(s)}."
    if s.get("description"):
        line += " " + s["description"]
    return line


def price_list(con):
    svcs = kb.services(con)
    keys = list(svcs)
    if svcs and all(sv.get("consultation") for sv in svcs.values()):
        p = kb.profile(con)
        names = [kb.soft_lower(svcs[k].get("plural") or svcs[k]["label"]) for k in keys if not k.startswith("not_sure")]
        return fmt(con, f"We build {', '.join(names[:-1])}{' and ' if len(names) > 1 else ''}{names[-1] if names else ''}. "
                        f"{p.get('pricing_note') or 'Every project is priced after a consultation.'}")
    parts = [f"{svcs[k]['label']}: {kb.price_text(svcs[k])}" for k in keys]
    return "Here's what we offer: " + " · ".join(parts) + "."


ASK = {
    "customer_name": "What name should I put the request under?",
    "preferred_date": "What day works best for you?",
    "preferred_time": "And what time works best for you?",
    "date_time": "What day and time work best for you?",
    "photos": "Could you send a screenshot or photo that shows what you mean?",
}


def service_question(con):
    svcs = kb.services(con)
    names = [kb.soft_lower(sv.get("plural") or sv["label"]) for k, sv in svcs.items() if not k.startswith("not_sure")]
    if not names:
        return "What can we help you with?"
    return ("What are you looking for help with: " + ", ".join(names[:-1]) + (", or " if len(names) > 1 else "") + names[-1] +
            "? Not sure yet is fine too.")


def ask_next(missing, ambiguity=None, limit=2, con=None):
    qs = []
    if "preferred_date" in missing and "preferred_time" in missing:
        missing = [("date_time" if m == "preferred_date" else m) for m in missing if m != "preferred_time"]
    for m in missing:
        q = service_question(con) if (m == "service" and con is not None) else ASK.get(m)
        if q and q not in qs:
            qs.append(q)
        if len(qs) >= limit:
            break
    out = " ".join(qs[:limit])
    return fmt(con, out) if con is not None else out


def _at_time(t):
    t = (t or "").strip()
    if not t:
        return ""
    if re.fullmatch(r"(morning|afternoon|evening)", t, re.I):
        return f" in the {t.lower()}"
    if re.match(r"(around|about|after|before|between|anytime|any time)\b", t, re.I):
        return f" {t}"
    return f" at {t}"


def draft_ack(con, fields):
    s = kb.services(con).get(fields.get("service_key") or "", {})
    owner = kb.profile(con)["owner"]
    first = (fields.get("customer_name") or "").split(" ")[0]
    if first.lower() == str(owner).lower():
        first = ""  # don't say "thanks Mola ... sent Mola"
    when = (fields.get("preferred_date") or "") + _at_time(fields.get("preferred_time"))
    alt = f" (backup: {fields['alt_date']})" if fields.get("alt_date") else ""
    topic = ""
    if s.get("label"):
        topic = s.get("phrase") or kb.soft_lower(s["label"])
    biz = fields.get("business")
    if biz and topic and not str(fields.get("service_key", "")).startswith("not_sure"):
        topic += f" for your {biz}"
    elif biz and not topic:
        topic = f"your {biz}"
    what = fmt(con, "{a_noun}") + (f" about {topic}" if topic else "")
    return fmt(con, f"Perfect, thanks{(' ' + first) if first else ''}! I've sent {{owner}} your request for {what} on {when}{alt}. "
                    "{She}'ll check {his} calendar and get back to you to confirm. It isn't booked until {she} confirms it.")


def confirmation_text(con, appt):
    f = appt["fields"] if isinstance(appt["fields"], dict) else workflow.fields_of(appt)
    s = kb.services(con, active_only=False).get(f.get("service_key") or "", {})
    first = (f.get("customer_name") or "").split(" ")[0]
    biz = kb.item(con, "business", "profile") or {}
    p = kb.profile(con)
    what = p.get("booking_noun") or "appointment"
    return (f"Hi{(' ' + first) if first else ''}! You're all set: your {what} with {p['owner']} is confirmed "
            f"for {f.get('preferred_date')} at {f.get('preferred_time')}. See you then!")


# ------------------------------------------------------------------ main entry
def handle(con, conversation_id, message_id):
    """Process one inbound customer message. Returns a dict describing what happened (for logs and tests)."""
    conv = con.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    msg = con.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    cust = con.execute("SELECT * FROM customers WHERE id=?", (conv["customer_id"],)).fetchone()
    text = (msg["text"] or "").strip()
    result = {"action": None, "intent": None, "escalation": None, "appointment_id": None, "reply_id": None}
    appt = workflow.open_for_conversation(con, conversation_id)
    appt = workflow.get(con, appt["id"]) if appt else None
    attachments = store.j(msg["attachments"], []) or []
    photos = [a.get("url") for a in attachments if a.get("type") in ("image", "photo") and a.get("url")]

    # booking state is recorded even when the AI is paused or the owner has the conversation, so drafts stay accurate
    if store.flag(con, "automation_paused"):
        result["action"] = "paused"
        return result
    if conv["mode"] == "human":
        result["action"] = "human_mode"
        return result

    intents = extract.intents(text)
    esc = extract.escalations(con, text)
    found = extract.booking_fields(con, text, store.parse_ts(msg["created_at"]))
    if photos:
        found["photos"] = photos
    svc_hint = found.get("_service")
    ambiguity = None
    cur_fields = dict(appt["fields"]) if appt else {}
    if not svc_hint and not cur_fields.get("service_key"):
        svc_hint = cur_fields.get("_service_hint")
        if not svc_hint:  # a service named earlier in the conversation ("how much is a CRM?" ... "let's book a call")
            for r in con.execute("SELECT text FROM messages WHERE conversation_id=? AND direction='in' AND id<? ORDER BY id DESC LIMIT 8",
                                 (conversation_id, message_id)):
                s_, _, _ = extract.resolve_service(con, r["text"] or "")
                if s_:
                    svc_hint = s_
                    break
    has_when = any(k in found for k in ("preferred_date", "preferred_time"))
    if "cancellation" in esc and re.search(r"\bpolic(y|ies)\b", text.lower()) and not re.search(r"\b(my|i need to|i have to|i want to)\b", text.lower()):
        esc.pop("cancellation")  # asking about the policy, not cancelling
        intents.append("policy")
    booking = "booking" in intents or bool(appt) or (bool(svc_hint) and has_when)
    consult = False

    merged = {**cur_fields, **{k: v for k, v in found.items() if not k.startswith("_")}}
    if not merged.get("customer_name") and cust["name"]:
        merged["customer_name"] = cust["name"]
    if cust["username"]:
        merged["instagram_username"] = "@" + cust["username"].lstrip("@")
    hint = svc_hint
    if hint:
        merged["_service"] = hint
        merged["service_key"] = hint
    if not merged.get("business"):  # "I run a property management company" said in an earlier message
        for r in con.execute("SELECT text FROM messages WHERE conversation_id=? AND direction='in' AND id<? ORDER BY id DESC LIMIT 8",
                             (conversation_id, message_id)):
            b = extract.business(r["text"] or "")
            if b:
                merged["business"] = b
                break
    if merged.get("business") and not merged.get("needs"):
        merged["needs"] = merged["business"][:1].upper() + merged["business"][1:]

    # model pass
    ai = None
    ai_meta = {}
    if ai_available():
        try:
            ai, ai_meta = _ask_model(con, conv, cust, text, merged, intents, esc, booking)
        except Exception as e:  # noqa: BLE001  (cap reached, network, bad JSON: fall back to templates)
            ai_meta = {"error": f"{e.__class__.__name__}: {str(e)[:200]}"}
            ai = None
    if ai:
        for k, v in (ai.get("extracted") or {}).items():
            if k in workflow.FIELD_NAMES and v not in (None, "", []) and k not in ("price_override_text", "price_override_reason", "photos"):
                if k == "service_key" and v not in kb.services(con):
                    continue
                if k == "customer_name" and not _grounded_name(v, conv, cust, con):
                    continue
                if k in merged and merged[k] and k in found:
                    continue  # rules already read it from this message
                merged[k] = str(v)[:300]
        if ai.get("intent") == "booking":
            booking = True
        if ai.get("escalate") and not esc:
            esc = {"model": [str(ai.get("escalate_reason") or "")[:200]]}

    # escalation wins
    cat = next((c for c in ESCALATION_ORDER if c in esc), None) or (next(iter(esc)) if esc else None)
    if cat:
        reason = ESCALATION_LABELS.get(cat, cat) + (f" ({', '.join(esc[cat][:3])})" if esc.get(cat) and cat != "model" else
                                                     (f": {esc[cat][0]}" if cat == "model" and esc[cat][0] else ""))
        con.execute("UPDATE conversations SET status='escalated', mode='human', escalation_reason=?, classification=?, updated_at=? WHERE id=?",
                    (reason, cat, store.now(), conversation_id))
        store.audit(con, "ai", "conversation.escalated", conversation_id, {"reason": reason})
        if appt:
            workflow.add_note(con, appt["id"], "ai", f"Customer message escalated to you: {reason}.")
        reply = fmt(con, HOLDING.get(cat, HOLDING["model"]))
        rid = outbox.enqueue(con, conversation_id, _with_intro(con, conv, reply), author="ai", intent="escalation", kind="holding",
                             ai_meta={"rules": esc, **ai_meta}, auto_ok=True)
        # escalation holding replies are always reviewed in review mode; in auto mode they may go out
        con.commit()
        result.update(action="escalated", escalation=reason, reply_id=rid, intent="escalation")
        return result

    # booking draft
    if booking or consult:
        if not appt:
            aid = workflow.create(con, conversation_id, cust["id"], kind="consultation" if consult else "booking", is_test=cust["is_test"])
            appt = workflow.get(con, aid)
        new_fields = {k: v for k, v in merged.items() if not k.startswith("_") and v != (appt["fields"] or {}).get(k)}
        summary = (ai or {}).get("summary") or _summary(con, merged, text)
        conf = _confidence(ai, merged, found)
        appt = workflow.ai_update(con, appt["id"], new_fields, summary=summary, confidence=conf)
        private = {k: v for k, v in (("_service_hint", svc_hint),) if v and appt["fields"].get(k) != v}
        if private and appt["status"] in workflow.AI_EDITABLE:
            # remember the service named earlier in the conversation
            f = appt["fields"]
            f.update(private)
            con.execute("UPDATE appointments SET fields=? WHERE id=?", (json.dumps(f), appt["id"]))
            con.commit()
            appt = workflow.get(con, appt["id"])
        if appt["status"] == workflow.INFORMATION_NEEDED:
            workflow.customer_replied(con, appt["id"])
            appt = workflow.get(con, appt["id"])
        result["appointment_id"] = appt["id"]
        missing = appt["missing"] if appt["missing"] else workflow.required_missing(con, appt["fields"], appt["kind"])
        if appt["status"] in (workflow.AWAITING_AVAILABILITY, workflow.READY_FOR_OWNER, workflow.OWNER_REVIEWING) and not missing:
            intent = "draft_ack"
            fallback = draft_ack(con, appt["fields"])
            just_created = any(e for e in con.execute("SELECT 1 FROM appointment_events WHERE appointment_id=? AND action='awaiting_availability'", (appt["id"],)))
            if not just_created:
                fallback = fmt(con, "Thanks! I've added that to your request. {owner} will check {his} calendar and get back to you to confirm.")
        else:
            intent = "consultation" if consult else "qualifying"
            lead = ""
            if "pricing" in intents and merged.get("service_key"):
                lead = service_line(con, merged["service_key"]) + " "
            elif "pricing" in intents:  # "how much?" before a service is picked: answer it, then keep qualifying
                note = kb.profile(con).get("pricing_note") or "{owner} goes over pricing with you directly."
                lead = fmt(con, note.rstrip(".") + ". " + ("Happy to set one up for you! " if not cur_fields else ""))
            elif not cur_fields:
                lead = fmt(con, "I'd be happy to help you set up {a_noun} with {owner}! ")
            fallback = (lead + ask_next(missing, ambiguity, con=con)).strip()
        reply_text = _pick(con, ai, fallback, appointment=appt)
        if "pricing" in intents and not re.search(r"pric|cost|scope|quote", reply_text, re.I):
            reply_text = fallback  # the price question must get an answer, not just more booking questions
        rid = outbox.enqueue(con, conversation_id, _with_intro(con, conv, reply_text), author="ai", intent=intent, ai_meta=ai_meta,
                             auto_ok=True, appointment_id=appt["id"])
        con.execute("UPDATE conversations SET classification=?, updated_at=? WHERE id=?", ("booking", store.now(), conversation_id))
        con.commit()
        result.update(action=intent, intent=intent, reply_id=rid)
        return result

    # information
    intent, fallback = _info_reply(con, text, intents, svc_hint, merged)
    if intent == "unknown":
        if ai and ai.get("reply") and ai.get("answerable") and not outbox.guard_problems(con, ai["reply"]):
            intent = "general"
        else:
            con.execute("UPDATE conversations SET status='escalated', mode='human', escalation_reason=?, classification='unknown', updated_at=? WHERE id=?",
                        (ESCALATION_LABELS["unknown"], store.now(), conversation_id))
            rid = outbox.enqueue(con, conversation_id, _with_intro(con, conv, fmt(con, HOLDING["unknown"])), author="ai", intent="escalation",
                                 kind="holding", ai_meta=ai_meta, auto_ok=True)
            con.commit()
            result.update(action="escalated", escalation=ESCALATION_LABELS["unknown"], reply_id=rid, intent="escalation")
            return result
    reply_text = _pick(con, ai, fallback)
    rid = outbox.enqueue(con, conversation_id, _with_intro(con, conv, reply_text), author="ai", intent=intent, ai_meta=ai_meta, auto_ok=True)
    con.execute("UPDATE conversations SET classification=?, updated_at=? WHERE id=?", (intent, store.now(), conversation_id))
    con.commit()
    result.update(action="answered", intent=intent, reply_id=rid)
    return result


def _info_reply(con, text, intents, svc_hint, merged):
    biz = kb.item(con, "business", "profile") or {}
    if "services" in intents and (not svc_hint or kb.services(con).get(svc_hint, {}).get("consultation") is None) and "pricing" not in intents and not svc_hint:
        p = kb.profile(con)
        return "services", (price_list(con) + fmt(con, " Would you like to {req}?").replace("{req}", p["request_phrase"])).strip()
    if "pricing" in intents or svc_hint:
        if merged.get("service_key"):
            return "pricing", service_line(con, merged["service_key"]) + fmt(con, " Would you like to {req}?").replace("{req}", kb.profile(con)["request_phrase"])
        if svc_hint:
            return "pricing", service_line(con, svc_hint) + fmt(con, " Would you like to {req}?").replace("{req}", kb.profile(con)["request_phrase"])
        return "pricing", price_list(con) + fmt(con, " Would you like to {req}?").replace("{req}", kb.profile(con)["request_phrase"])
    if "policy" in intents:
        pols = {k: (kb.item(con, "policy", k) or {}).get("text") for k in ("cancellation", "deposit", "late_arrival", "no_show")}
        asked = [k for k in pols if k.replace("_", " ").split()[0] in text.lower() or (k == "no_show" and "no show" in text.lower().replace("-", " "))]
        have = [pols[k] for k in (asked or ["cancellation"]) if pols.get(k)]
        return ("policy", " ".join(have)) if have else ("unknown", "")
    if "preparation" in intents:
        t = (kb.item(con, "prep", "general") or {}).get("text")
        return ("preparation", t) if t else ("unknown", "")
    if "aftercare" in intents:
        t = (kb.item(con, "aftercare", "general") or {}).get("text")
        return ("aftercare", t) if t else ("unknown", "")
    if "hours" in intents:
        return ("hours", fmt(con, f"{{owner_s}} hours: {biz['hours']}")) if biz.get("hours") else ("unknown", "")
    if "location" in intents:
        if biz.get("address"):
            return "location", fmt(con, f"{{brand}} is at {biz['address']}.")
    for faq in kb.all_items(con)["faq"].values():
        q = str(faq.get("question") or "").lower()
        keys = [w for w in re.findall(r"[a-z']{4,}", q) if w not in ("what", "your", "does", "with", "have", "will", "should", "when", "there", "about", "this", "that")]
        if keys and sum(1 for w in keys if w in text.lower()) >= max(2, len(keys) // 2):
            return "faq", faq["answer"]
    if "thanks" in intents:
        return "thanks", "You're welcome! Let me know if there's anything else I can help with."
    if "greeting" in intents:
        p = kb.profile(con)
        return "greeting", (f"Hi! Thanks for reaching out to {p['brand']}. How can I help? I can answer questions about {p['offer_phrase']}, "
                            f"or help you {p['request_phrase']}.")
    return "unknown", ""


def _with_intro(con, conv, text):
    already = conv["intro_sent"] or con.execute("SELECT 1 FROM messages WHERE conversation_id=? AND direction='out' AND author IN ('ai','ai_reviewed') "
                                                 "AND status NOT IN ('discarded','blocked')", (conv["id"],)).fetchone()
    if store.flag(con, "intro_line") and not already:
        return kb.profile(con)["assistant_intro"].rstrip() + " " + re.sub(r"^(hi|hey|hello)\b[!,.]?\s*", "", text, flags=re.I)
    return text


def _pick(con, ai, fallback, appointment=None):
    """Use the model's reply only if it passes every guard; otherwise the template."""
    if ai and ai.get("reply"):
        r = str(ai["reply"]).strip()
        if not outbox.guard_problems(con, r, appointment=appointment):
            return r
    return fallback


def _grounded_name(v, conv, cust, con):
    first = str(v).split(" ")[0].lower()
    if cust["name"] and first in cust["name"].lower():
        return True
    texts = " ".join((r["text"] or "") for r in con.execute("SELECT text FROM messages WHERE conversation_id=? AND direction='in'", (conv["id"],)))
    return bool(first) and re.search(r"\b" + re.escape(first) + r"\b", texts.lower()) is not None


def _summary(con, f, last):
    s = kb.services(con, active_only=False).get(f.get("service_key") or "", {})
    bits = []
    if f.get("customer_name"):
        bits.append(f["customer_name"])
    bits.append(s.get("label") or "service not chosen yet")
    if f.get("preferred_date"):
        bits.append("prefers " + f["preferred_date"] + (f" ({f['preferred_time']})" if f.get("preferred_time") else ""))
    if f.get("alt_date"):
        bits.append("backup " + f["alt_date"])
    return "; ".join(bits) + f'. Last message: "{last[:160]}"'


def _confidence(ai, merged, found):
    base = 0.55
    if ai and isinstance(ai.get("confidence"), (int, float)):
        base = float(ai["confidence"])
    stated = sum(1 for k in ("customer_name", "preferred_date", "preferred_time", "service_key") if merged.get(k))
    return round(max(0.1, min(0.99, base * 0.7 + stated / 4 * 0.3)), 2)


def _ask_model(con, conv, cust, text, merged, intents, esc, booking):
    from .. import agents_ai
    hist = [{"from": "customer" if r["direction"] == "in" else ("owner" if r["author"] in ("owner", "operator") else "assistant"),
             "text": (r["text"] or "")[:600]}
            for r in con.execute("SELECT direction,author,text FROM messages WHERE conversation_id=? AND status NOT IN ('discarded','blocked') "
                                 "ORDER BY id DESC LIMIT ?", (conv["id"], HISTORY))][::-1]
    missing = workflow.required_missing(con, merged)
    first_person = store.get(con, "voice_person") == "first_person"
    p = kb.profile(con)
    system = (f"You are the Instagram assistant for {p['brand']}. {p.get('about') or ''} "
              + (f"You may write in first person as {p['owner_possessive']} business. " if first_person else
                 f"You speak as {p['owner_possessive']} assistant, never as {p['owner']} personally, and never invent personal experiences, "
                 "results, clients or numbers. ")
              + "Tone: natural, friendly, professional, concise. At most one emoji. No AI disclaimers beyond the intro line. No sales pressure.")
    price_rule = "services marked consultation_required have no fixed price: say pricing is scoped on a call, never give numbers. "
    instructions = fmt(con, (
        "Read the customer's latest message in context and do three things.\n"
        "1) intent: one of booking, pricing, services, preparation, aftercare, hours, location, faq, greeting, thanks, complaint, cancellation, "
        "reschedule, existing_appointment, refund, discount, other.\n"
        "2) extracted: details the CUSTOMER stated (never guess): customer_name, service_key (one of the service keys in the data), "
        "needs (their company, industry and what they need help with), preferred_date, preferred_time, "
        "alt_date, alt_time, notes, contact (only if they offered it).\n"
        "3) reply: the next Instagram message (under 600 characters).\n"
        "Hard rules for the reply: use ONLY prices, services, policies and facts in the data; never state a price, duration or policy that isn't "
        "in it; never say a time or day is available or open (only {owner} checks {his} calendar); never say {a_noun} is booked, scheduled or "
        "confirmed; for a {noun} request, ask only for missing details (at most two questions) and don't re-ask what's known; if a request is "
        "complete, say you've passed it to {owner}, {she}'ll check {his} calendar and confirm, and it isn't booked until {she} does; "
        + price_rule + "If the question can't be answered from the data, set answerable=false.\n"
        "Set escalate=true (with escalate_reason) for complaints, refunds, discounts or pricing exceptions, cancellations, changes to an existing "
        "booking, sensitive or legal topics, or anything that needs {owner_s} judgment."))
    data = {"knowledge": kb.facts_for_ai(con), "conversation": hist, "latest_customer_message": text,
            "known_booking_details": {k: v for k, v in merged.items() if not k.startswith("_")}, "still_missing_for_request": missing,
            "rule_signals": {"intents": intents, "escalation_triggers": list(esc), "booking_in_progress": bool(booking)},
            "customer_profile": {"instagram_username": cust["username"], "name_on_profile": cust["name"]}}
    spec = ('{"intent": str, "extracted": {field: value}, "reply": str, "answerable": bool, "escalate": bool, "escalate_reason": str, '
            '"summary": "one sentence for the owner", "confidence": 0.0-1.0, "needs_info": []}')
    ai_con = _ai_con()
    tenant = store.tenant_of(con) or "client"
    parsed, meta = agents_ai.ask(ai_con, agent=f"client:{tenant}", kind="igassist.reply", task_id=f"conv{conv['id']}", business="demigod",
                                 tier="fast", system=system, instructions=instructions, data=data, output_spec=spec, max_tokens=900)
    if not isinstance(parsed, dict):
        raise ValueError("model reply wasn't JSON")
    return parsed, {"model": meta.get("model"), "cost": meta.get("cost"), "ai_intent": parsed.get("intent"), "ai_reply": str(parsed.get("reply") or "")[:1000]}
