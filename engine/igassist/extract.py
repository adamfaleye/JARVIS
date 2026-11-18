"""Deterministic reading of customer messages: intents, escalation triggers, booking details.

The model (when available) improves wording and catches what these rules miss, but these rules always run and
always win on safety: an escalation found here can't be overruled by the model.
"""
import re
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    TZ = None

from . import kb

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
MON_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}

BOOKING_WORDS = ["book", "booking", "appointment", "appt", "schedule", "availability", "available", "opening", "openings",
                 "slot", "come in", "get in", "fit me in", "squeeze me in", "next week", "this week", "when can", "are you free",
                 "reserve", "make an appointment", "can i get", "could i get", "talk to mola", "speak with mola", "speak to mola",
                 "book a call", "schedule a call", "hop on a call", "jump on a call", "get on a call", "discovery call", "set up a call",
                 "set up a meeting", "schedule a meeting", "book a meeting", "a demo", "demo call", "quick call", "strategy call", "consultation call"]
PRICE_WORDS = ["price", "prices", "pricing", "cost", "costs", "how much", "rate", "rates", "charge", "fee", "menu", "$"]
PREP_WORDS = ["prepare", "preparation", "before the call", "before our call", "what do i need for the call", "should i bring",
              "what should i have ready"]
SERVICES_WORDS = ["what do you do", "what do you guys do", "what do y'all do", "what do you build", "what do you offer", "what services",
                  "your services", "what can you do", "what can you build", "tell me more", "how does it work", "how does this work",
                  "what is ai sidekicks", "what's ai sidekicks", "who are you", "what kind of", "do you build", "do you make", "can you build",
                  "can you make", "do you offer", "do you do"]
AFTER_WORDS = ["aftercare", "after my cut", "after the cut", "maintain", "maintenance", "routine", "products should i"]
GREETINGS = ["hi", "hello", "hey", "hiya", "good morning", "good afternoon", "good evening", "yo"]
THANKS = ["thanks", "thank you", "thx", "ty", "appreciate it", "perfect", "great", "awesome", "ok", "okay", "sounds good"]
HOURS_WORDS = ["hours", "open on", "open today", "what time do you open", "what time do you close", "are you open"]
LOCATION_WORDS = ["where are you", "address", "location", "located", "parking"]


def _has(text, phrase):
    t = text.lower()
    p = phrase.lower()
    if not re.search(r"[a-z0-9]", p):
        return p in t
    return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t) is not None


def any_of(text, phrases):
    return [p for p in phrases if _has(text, p)]


def escalations(con, text):
    rules = kb.item(con, "escalation", "rules") or {}
    found = {}
    for cat, words in rules.items():
        hits = any_of(text, words or [])
        if hits:
            found[cat] = hits
    # "free" alone is ambiguous ("are you free Friday?"), but "for free"/"free cut" is a discount request
    if re.search(r"\b(for free|free (build|setup|service|month|trial))\b", text.lower()):
        found.setdefault("discount", []).append("free")
    return found


def resolve_service(con, text, known=None):
    """Returns (service_key or None, None, None). The knowledge base's words for each service decide; a specific service
    wins over "not sure"."""
    t = text.lower()
    hits = []
    for key, s in kb.services(con).items():
        for a in [s["label"].lower()] + (s.get("aliases") or []):
            if _has(t, a):
                hits.append(key)
                break
    hits = list(dict.fromkeys(hits))
    if not hits:
        return None, None, None
    specific = [h for h in hits if not h.startswith("not_sure")]
    return (specific or hits)[0], None, None


def _today(ref=None):
    if ref:
        return ref.date() if isinstance(ref, datetime) else ref
    return datetime.now(TZ).date() if TZ else date.today()


def dates(text, ref=None):
    """All dates mentioned, in order: [(display_text, iso or None)]."""
    t = text.lower()
    today = _today(ref)
    out = []
    pats = []
    for m in re.finditer(r"\b(today|tonight|tomorrow|tmrw|tmr)\b", t):
        d = today if m.group(1) in ("today", "tonight") else today + timedelta(days=1)
        pats.append((m.start(), m.group(0), d))
    for m in re.finditer(r"\b(next|this)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thur?s?|fri|sat|sun)\b\.?", t):
        word = m.group(2)
        wd = WEEKDAYS.index(word) if word in WEEKDAYS else WD_ABBR.get(word)
        if wd is None:
            continue
        if word not in WEEKDAYS and m.group(1) is None:
            # short forms ("sat", "wed") only count after "on", "or", "and" or a comma, so "sat down" isn't a Saturday
            if not re.search(r"(\bon|\bor|\band|,)\s*$", t[:m.start()]):
                continue
        delta = (wd - today.weekday()) % 7
        if m.group(1) == "next":
            delta = delta + 7 if delta < 7 and delta != 0 else (delta or 7)
            if delta > 13:
                delta -= 7
        elif delta == 0 and m.group(1) != "this":
            delta = 0
        pats.append((m.start(), m.group(0).strip(), today + timedelta(days=delta)))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        mo, dy = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) + (2000 if m.group(3) and len(m.group(3)) == 2 else 0) if m.group(3) else today.year
        try:
            d = date(yr, mo, dy)
            if not m.group(3) and d < today:
                d = date(yr + 1, mo, dy)
            pats.append((m.start(), m.group(0), d))
        except ValueError:
            pass
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t):
        try:
            mo = MON_ABBR[m.group(1)[:4] if m.group(1).startswith("sept") else m.group(1)[:3]]
            d = date(today.year, mo, int(m.group(2)))
            if d < today:
                d = date(today.year + 1, mo, int(m.group(2)))
            pats.append((m.start(), m.group(0), d))
        except (ValueError, KeyError):
            pass
    for m in re.finditer(r"\bthe\s+(\d{1,2})(st|nd|rd|th)\b", t):
        dy = int(m.group(1))
        try:
            d = date(today.year, today.month, dy)
            if d < today:
                d = date(today.year + (today.month == 12), today.month % 12 + 1, dy)
            pats.append((m.start(), m.group(0), d))
        except ValueError:
            pass
    seen = set()
    for _, txt, d in sorted(pats, key=lambda x: x[0]):
        if d.isoformat() in seen:
            continue
        seen.add(d.isoformat())
        out.append((f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}", d.isoformat()))
    return out


def times(text):
    t = text.lower()
    out = []
    for m in re.finditer(r"\b(?:(before|after|around|at|by)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", t):
        out.append((m.start(), " ".join(x for x in [m.group(1), f"{m.group(2)}{':' + m.group(3) if m.group(3) else ''} {m.group(4).replace('.', '')}"] if x)))
    for m in re.finditer(r"\b(?:(before|after|around)\s+)(\d{1,2})(?::(\d{2}))?\b(?!\s*(am|pm|/|\d))", t):
        out.append((m.start(), f"{m.group(1)} {m.group(2)}{':' + m.group(3) if m.group(3) else ''}"))
    for m in re.finditer(r"\b(early morning|late morning|morning|midday|noon|lunchtime|early afternoon|late afternoon|afternoon|evening|after work|anytime|any time|flexible|whenever)\b", t):
        out.append((m.start(), m.group(1)))
    seen, res = set(), []
    for _, v in sorted(out):
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res


def name(text):
    m = re.search(r"\b(?i:my name is|my name's|name is|name's|it's|i am|i'm|im)\s+([A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+)?)", text)
    if not m:
        return None
    n = m.group(1).strip()
    if n.lower().split()[0] in {"curious", "interested", "looking", "new", "free", "available", "wondering", "hoping",
                                  "trying", "so", "not", "just", "a", "the", "here", "back", "good", "ok", "okay", "sorry", "going"}:
        return None
    return n


def contact(text):
    out = {}
    e = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if e:
        out["email"] = e.group(0)
    p = re.search(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)", text)
    if p:
        out["phone"] = p.group(0)
    return out


def intents(text):
    t = text.lower().strip()
    found = []
    if any_of(t, BOOKING_WORDS):
        found.append("booking")
    if any_of(t, PRICE_WORDS):
        found.append("pricing")
    if any_of(t, SERVICES_WORDS):
        found.append("services")
    if any_of(t, PREP_WORDS):
        found.append("preparation")
    if any_of(t, AFTER_WORDS):
        found.append("aftercare")
    if re.search(r"\bpolic(y|ies)\b", t) or re.search(r"\b(deposit|no[- ]show)\b", t):
        found.append("policy")
    if any_of(t, HOURS_WORDS):
        found.append("hours")
    if any_of(t, LOCATION_WORDS):
        found.append("location")
    words = re.sub(r"[^a-z' ]", " ", t).split()
    if words and len(words) <= 4 and any(" ".join(words[:2]).startswith(g) or words[0] == g for g in GREETINGS):
        found.append("greeting")
    if words and len(words) <= 5 and any_of(t, THANKS):
        found.append("thanks")
    return found


_BIZ = re.compile(r"\b(?:i|we)\s+(?:run|own|operate|manage|have|started|work at|work for)\s+(?:a|an|my|our|the)\s+"
                  r"((?:[a-z0-9&'\-]+\s+){0,4}?(?:company|business|firm|agency|brokerage|practice|shop|restaurant|startup|portfolio|team|store|salon|clinic|office))\b",
                  re.I)


def business(text):
    """'I run a property management company' -> 'property management company' (lowercase), else None."""
    m = _BIZ.search(text or "")
    if not m:
        return None
    b = re.sub(r"\s+", " ", m.group(1)).strip().lower()
    return b if 3 <= len(b) <= 60 else None


def booking_fields(con, text, ref=None):
    """Booking details stated in one message."""
    f = {}
    svc, _, _ = resolve_service(con, text)
    if svc:
        f["_service"] = svc
    ds = dates(text, ref)
    if ds:
        f["preferred_date"], f["preferred_date_iso"] = ds[0]
        if len(ds) > 1:
            f["alt_date"] = ds[1][0]
    ts = times(text)
    if ts:
        f["preferred_time"] = ts[0]
        if len(ts) > 1:
            f["alt_time"] = ts[1]
    n = name(text)
    if n:
        f["customer_name"] = n
    b = business(text)
    if b:
        f["business"] = b
    c = contact(text)
    if c:
        f["contact"] = ", ".join(c.values())
    return f


def finalize_service(fields):
    return fields.get("_service")
