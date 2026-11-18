"""Editable knowledge base. The assistant may only state what is in here; nothing is invented.

Seeded from facts Mola has stated about AI Sidekicks. Anything not provided yet is stored as missing (value None)
and listed in the setup checklist, so the assistant hands the question to Mola instead of guessing.
"""
import json

from . import store

KINDS = ("service", "faq", "policy", "business", "voice", "escalation", "prep", "aftercare", "discovery")

# AI Sidekicks (@aisidekicks): only facts Mola has stated (positioning brief, Nov 2026). No prices are published:
# every service is scoped on a discovery call, so all are marked consultation (no fixed price).
SEED_AISIDEKICKS = {
    "service": {
        "ai_assistant": {
            "label": "Custom AI assistant", "plural": "Custom AI assistants", "price_cents": None, "consultation": True,
            "phrase": "a custom AI assistant",
            "pitch": "We build custom AI assistants that handle the calls, texts, DMs and follow-ups your team does by hand.",
            "duration_min": None, "requires_photos": False, "active": True,
            "description": "A custom AI assistant built around how your business works: answering calls, texts, DMs or email, "
                           "booking, follow-ups and other repetitive work, working alongside your team.",
            "aliases": ["ai assistant", "assistant", "chatbot", "chat bot", "receptionist", "ai receptionist", "voice agent", "phone agent",
                        "answer calls", "answer my calls", "dm assistant", "instagram assistant", "booking assistant", "agent", "ai agent",
                        "assistants", "chatbots", "receptionists", "ai receptionists", "agents", "ai agents", "voice agents"],
        },
        "custom_crm": {
            "label": "Custom CRM", "plural": "Custom CRMs", "price_cents": None, "consultation": True,
            "phrase": "a custom CRM",
            "pitch": "We build custom CRMs around how your team actually works, including for property managers.",
            "duration_min": None, "requires_photos": False, "active": True,
            "description": "A CRM built for your business and workflow, including for property management owners.",
            "aliases": ["crm", "crms", "custom crm", "custom crms", "customer database", "pipeline", "client management", "lead tracking"],
        },
        "automation": {
            "label": "Business automation", "plural": "Business automation", "price_cents": None, "consultation": True,
            "phrase": "automating your back-office work",
            "pitch": "We automate the repetitive back-office work your team does by hand.",
            "duration_min": None, "requires_photos": False, "active": True,
            "description": "Automating the repetitive back-office work your team does by hand.",
            "aliases": ["automation", "automate", "automations", "workflow", "workflows", "back office", "zapier", "integration", "integrations"],
        },
        "not_sure": {
            "label": "Not sure yet (talk it through)", "price_cents": None, "consultation": True,
            "phrase": "what AI could do for your business",
            "pitch": "A discovery call is the easiest way to figure out what would help most.",
            "duration_min": None, "requires_photos": False, "active": True,
            "description": "Not sure what you need yet? A discovery call is for exactly that.",
            "aliases": ["not sure", "no idea", "something else", "help with ai", "use ai", "ai for my business", "what can you do", "consultation"],
        },
    },
    "business": {
        "profile": {"brand": "AI Sidekicks", "instagram": "@aisidekicks", "owner": "Mola", "industry": "agency",
                    "subtitle": "Mola · AI Sidekicks", "booking_noun": "discovery call", "calendar": "your calendar",
                    "assistant_intro": "Hi! I'm the AI Sidekicks assistant.",
                    "about": ("AI Sidekicks builds custom AI assistants, custom CRMs and business automation for small and midsize "
                              "businesses (about 2-250 employees) across industries, with a focus on real estate, HVAC, plumbing and "
                              "roofing. Core message: custom AI assistants that give businesses their time back. "
                              "Tagline: Intelligence Built Around Your Business. Sales are consultative: every project starts with a discovery call."),
                    "tagline": "Intelligence Built Around Your Business",
                    "notes_label": "Business and what they need", "pronouns": ["he", "him", "his"],
                    "offer_phrase": "what we build and how it works", "request_phrase": "set up a discovery call with Mola",
                    "requests_label": "Call Requests", "baseline_hint": "",
                    "calendar_note": ("JARVIS doesn't connect to your calendar yet. Put approved calls in your calendar yourself, "
                                      "and JARVIS records that you did."),
                    "booking_method": "Mola books every discovery call personally.",
                    "hours": None, "address": None, "city": None, "new_client_requirements": None,
                    "pricing_note": "Pricing depends on what you need, so Mola scopes it with you on a discovery call."},
    },
    "voice": {
        "style": {"notes": None, "traits": ["natural", "conversational", "friendly", "professional", "knowledgeable", "concise", "confident", "consultative"],
                  "avoid": ["robotic scripts", "excessive emojis", "hype or aggressive sales language", "made-up results or client names",
                            "pretending to be Mola"], "max_emojis": 1, "sample_replies": []},
    },
    "policy": {"deposit": {"text": None}, "cancellation": {"text": None}, "late_arrival": {"text": None}, "no_show": {"text": None}},
    "prep": {"general": {"text": None}},
    "aftercare": {"general": {"text": None}},
    "faq": {},
    "escalation": {
        "rules": {
            "always_personal": [],
            "complaint": ["complain", "complaint", "unhappy", "not happy", "disappointed", "terrible", "worst", "scam", "angry", "rude", "awful"],
            "refund": ["refund", "money back", "charge back", "chargeback", "reimburse"],
            "discount": ["discount", "coupon", "promo", "cheaper", "% off", "percent off", "price match", "special price"],
            "cancellation": ["cancel my call", "cancel the call", "cancel our call", "cancel my meeting", "can't make the call", "cant make the call"],
            "reschedule": ["reschedule", "move our call", "move my call", "different time for the call"],
            "existing_appointment": ["my call", "our call", "my meeting", "our meeting", "am i booked"],
            "sensitive": ["lawyer", "sue", "legal action", "contract dispute", "invoice dispute"],
            "uncertain": ["guarantee", "guaranteed", "how much exactly", "exact price", "quote me", "nda", "partnership", "investor", "invest"],
        },
    },
    "discovery": {
        "checklist": {"items": [
            {"id": "faqs", "label": "Common questions people DM you, with your answers", "done": False},
            {"id": "always_personal", "label": "Topics you always want to handle yourself", "done": False},
            {"id": "voice", "label": "Your voice and sample replies", "done": False},
            {"id": "durations", "label": "Discovery call length (set it on each service)", "done": False},
            {"id": "pricing", "label": "Anything about pricing you're OK sharing publicly (otherwise: scoped on a call)", "done": False},
            {"id": "proof", "label": "Real results or case studies you're allowed to mention (none yet)", "done": False},
        ]},
    },
}

SEEDS = {"aisidekicks": SEED_AISIDEKICKS}
SEED = SEED_AISIDEKICKS

PROFILE_DEFAULTS = {"brand": "Client", "owner": "the owner", "subtitle": "", "booking_noun": "appointment", "calendar": "your calendar",
                    "assistant_intro": "Hi! I'm the booking assistant.", "about": "",
                    "notes_label": "Notes", "industry": "business", "pronouns": ["they", "them", "their"],
                    "offer_phrase": "services and pricing", "request_phrase": "request an appointment", "pricing_note": "",
                    "requests_label": "Requests", "baseline_hint": "", "calendar_note": "Bookings are entered in the calendar by hand."}


def profile(con):
    """Per-client wording: brand, owner who approves, what a booking is called, the calendar it goes into."""
    p = dict(PROFILE_DEFAULTS)
    p.update({k: v for k, v in (item(con, "business", "profile") or {}).items() if v not in (None, "")})
    p["owner_possessive"] = p["owner"] + ("'" if p["owner"].endswith("s") else "'s")
    return p


def fmt(con, text):
    """Fill in the client's wording: {owner} {owner_s} {she} {him} {his} {She} {brand} {noun} {cal} {a_noun}."""
    p = profile(con)
    she, him, his = (p.get("pronouns") or ["they", "them", "their"])[:3]
    noun = p.get("booking_noun") or "appointment"
    vals = {"owner": p["owner"], "owner_s": p["owner_possessive"], "she": she, "him": him, "his": his, "She": she.capitalize(),
            "brand": p["brand"], "noun": noun, "Noun": noun[:1].upper() + noun[1:], "cal": p.get("calendar") or "your calendar",
            "a_noun": ("an " if noun[:1].lower() in "aeiou" else "a ") + noun}
    for k, v in vals.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def seed(con, actor="seed"):
    """Adds missing entries. Never overwrites an edited entry; the business profile only gains keys it doesn't have yet."""
    n = 0
    tenant = store.tenant_of(con) or "aisidekicks"
    seeds = SEEDS.get(tenant, SEED_AISIDEKICKS)
    for kind, items in seeds.items():
        for key, data in items.items():
            cur = con.execute("INSERT OR IGNORE INTO kb_items(kind,key,data,version,updated_at,updated_by) VALUES(?,?,?,1,?,?)",
                              (kind, key, json.dumps(data), store.now(), actor))
            n += cur.rowcount
    for key, data in seeds.get("service", {}).items():  # wording fields added in later versions, never overwriting edits
        cur_s = item(con, "service", key)
        add = {k: v for k, v in data.items() if k in ("phrase", "pitch") and cur_s is not None and k not in cur_s}
        if add:
            con.execute("UPDATE kb_items SET data=? WHERE kind='service' AND key=?", (json.dumps({**cur_s, **add}), key))
    have = item(con, "business", "profile")
    want = seeds.get("business", {}).get("profile", {})
    if have is not None and any(k not in have for k in want):
        merged = {**{k: v for k, v in want.items() if k not in have}, **have}
        con.execute("UPDATE kb_items SET data=? WHERE kind='business' AND key='profile'", (json.dumps(merged),))
    con.commit()
    return n


def all_items(con):
    out = {k: {} for k in KINDS}
    for r in con.execute("SELECT kind,key,data,version,updated_at,updated_by FROM kb_items ORDER BY kind,key"):
        out.setdefault(r["kind"], {})[r["key"]] = {**json.loads(r["data"]), "_version": r["version"],
                                                    "_updated_at": r["updated_at"], "_updated_by": r["updated_by"]}
    return out


def item(con, kind, key):
    r = con.execute("SELECT data FROM kb_items WHERE kind=? AND key=?", (kind, key)).fetchone()
    return json.loads(r["data"]) if r else None


def services(con, active_only=True):
    out = {}
    for r in con.execute("SELECT key,data FROM kb_items WHERE kind='service' ORDER BY key"):
        d = json.loads(r["data"])
        if d.get("active", True) or not active_only:
            out[r["key"]] = d
    return out


def soft_lower(label):
    """'Custom AI assistant' -> 'custom AI assistant' (keeps acronyms)."""
    return label[:1].lower() + label[1:] if label else label


def price_text(svc):
    if svc.get("consultation") or svc.get("price_cents") is None:
        return "Consultation required"
    c = int(svc["price_cents"])
    return f"${c // 100}" if c % 100 == 0 else f"${c / 100:.2f}"


def approved_prices(con):
    """Every dollar figure the assistant is allowed to say."""
    return {int(s["price_cents"]) for s in services(con).values() if s.get("price_cents") is not None and not s.get("consultation")}


def _clean(kind, key, data):
    if kind not in KINDS:
        raise ValueError("Unknown knowledge type.")
    if not isinstance(data, dict):
        raise ValueError("Knowledge entries are objects.")
    if not key or len(key) > 60 or not all(ch.isalnum() or ch in "_-" for ch in key):
        raise ValueError("Keys are letters, digits, _ or -.")
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    if kind == "service":
        if not str(data.get("label") or "").strip():
            raise ValueError("A service needs a name.")
        pc = data.get("price_cents")
        if pc in ("", None):
            data["price_cents"] = None
            if not data.get("consultation"):
                raise ValueError("A service without a price must be marked consultation-required.")
        else:
            try:
                pc = int(pc)
            except (TypeError, ValueError):
                raise ValueError("Price must be a whole number of cents.")
            if not 0 <= pc <= 500000:
                raise ValueError("Price must be between $0 and $5,000.")
            data["price_cents"] = pc
        dm = data.get("duration_min")
        if dm not in (None, ""):
            dm = int(dm)
            if not 5 <= dm <= 600:
                raise ValueError("Duration must be 5-600 minutes.")
            data["duration_min"] = dm
        else:
            data["duration_min"] = None
        data["aliases"] = [str(a).strip().lower() for a in (data.get("aliases") or []) if str(a).strip()][:30]
    if kind == "escalation":
        for k, v in data.items():
            if not isinstance(v, list):
                raise ValueError(f"Escalation rule '{k}' must be a list of words or phrases.")
            data[k] = [str(x).strip().lower() for x in v if str(x).strip()][:100]
    if kind == "faq":
        if not str(data.get("question") or "").strip() or not str(data.get("answer") or "").strip():
            raise ValueError("An FAQ needs a question and an answer.")
    if len(json.dumps(data)) > 20000:
        raise ValueError("Entry is too long.")
    return data


def save(con, kind, key, data, actor, expected_version=None):
    data = _clean(kind, key, data)
    r = con.execute("SELECT data,version FROM kb_items WHERE kind=? AND key=?", (kind, key)).fetchone()
    if r and expected_version is not None and int(expected_version) != r["version"]:
        raise ValueError("Someone changed this entry since you opened it. Reload and try again.")
    if r:
        con.execute("INSERT INTO kb_history(kind,key,data,version,at,by) VALUES(?,?,?,?,?,?)",
                    (kind, key, r["data"], r["version"], store.now(), actor))
        con.execute("UPDATE kb_items SET data=?, version=version+1, updated_at=?, updated_by=? WHERE kind=? AND key=?",
                    (json.dumps(data), store.now(), actor, kind, key))
    else:
        con.execute("INSERT INTO kb_items(kind,key,data,version,updated_at,updated_by) VALUES(?,?,?,1,?,?)",
                    (kind, key, json.dumps(data), store.now(), actor))
    store.audit(con, actor, "kb.save", f"{kind}/{key}", {"version": (r["version"] + 1) if r else 1})
    con.commit()
    return item(con, kind, key)


def delete(con, kind, key, actor):
    r = con.execute("SELECT data,version FROM kb_items WHERE kind=? AND key=?", (kind, key)).fetchone()
    if not r:
        return False
    if kind in ("discovery", "business", "voice", "escalation"):
        raise ValueError("This entry can be edited but not deleted.")
    con.execute("INSERT INTO kb_history(kind,key,data,version,at,by) VALUES(?,?,?,?,?,?)", (kind, key, r["data"], r["version"], store.now(), actor))
    con.execute("DELETE FROM kb_items WHERE kind=? AND key=?", (kind, key))
    store.audit(con, actor, "kb.delete", f"{kind}/{key}")
    con.commit()
    return True


def missing_facts(con):
    """Facts the assistant must not state because the owner hasn't provided them yet."""
    out = []
    biz = item(con, "business", "profile") or {}
    for f, label in (("hours", "business hours"), ("address", "business address"), ("new_client_requirements", "new-client requirements")):
        if not biz.get(f):
            out.append(label)
    for k in ("deposit", "cancellation", "late_arrival", "no_show"):
        if not (item(con, "policy", k) or {}).get("text"):
            out.append(f"{k.replace('_', ' ')} policy")
    for k, lbl in (("prep", "preparation instructions"), ("aftercare", "aftercare instructions")):
        if not (item(con, k, "general") or {}).get("text"):
            out.append(lbl)
    for key, s in services(con).items():
        if not s.get("duration_min"):
            out.append(f"{s['label']} duration")
    return out


def facts_for_ai(con):
    """Compact, verified-only view handed to the model."""
    svcs = []
    for key, s in services(con).items():
        svcs.append({"key": key, "name": s["label"], "price": price_text(s), "consultation_required": bool(s.get("consultation")),
                     "duration": f"{s['duration_min']} minutes" if s.get("duration_min") else "not provided yet",
                     "includes": s.get("includes") or [], "description": s.get("description") or "",
                     "photos_needed": s.get("requires_photos")})
    pol = {k: (item(con, "policy", k) or {}).get("text") for k in ("deposit", "cancellation", "late_arrival", "no_show")}
    faqs = [{"q": d["question"], "a": d["answer"]} for d in all_items(con)["faq"].values() if d.get("question") and d.get("answer")]
    return {"business": item(con, "business", "profile"), "services": svcs, "policies": pol, "faqs": faqs,
            "preparation": (item(con, "prep", "general") or {}).get("text"), "aftercare": (item(con, "aftercare", "general") or {}).get("text"),
            "voice": item(con, "voice", "style"), "not_provided_yet": missing_facts(con)}
