"""Appointment workflow. Every status change goes through transition(); nothing else writes appointments.status.

Rules enforced here (not in the UI):
- Only the owner (role "owner") can verify availability, approve, record the calendar entry, confirm, decline or cancel.
  An operator can view and edit the knowledge base but can't finalize a request.
- The AI and background workers ("system") can only make the automatic early moves: inquiry -> qualifying ->
  draft created -> awaiting availability, and back from "information needed" when the customer replies.
- Approval is bound to the exact booking details (hash). Editing after approval voids it.
- A draft is not a booking, and approval is not a calendar booking: the customer can be told "you're booked" only
  after the owner records the calendar entry AND confirms it is on the calendar (status CONFIRMED).
"""
import hashlib
import json

from . import kb, store

NEW_INQUIRY = "NEW_INQUIRY"
QUALIFYING = "QUALIFYING"
DRAFT_CREATED = "DRAFT_CREATED"
AWAITING_AVAILABILITY = "AWAITING_AVAILABILITY"
READY_FOR_OWNER = "READY_FOR_OWNER"
OWNER_REVIEWING = "OWNER_REVIEWING"
APPROVED = "APPROVED"
AWAITING_CALENDAR_ENTRY = "AWAITING_CALENDAR_ENTRY"
ENTERED_IN_CALENDAR = "ENTERED_IN_CALENDAR"
CONFIRMED = "CONFIRMED"
INFORMATION_NEEDED = "INFORMATION_NEEDED"
DECLINED = "DECLINED"
CANCELLED = "CANCELLED"
COMPLETED = "COMPLETED"

LABEL_TEMPLATES = {
    NEW_INQUIRY: "New inquiry", QUALIFYING: "Qualifying", DRAFT_CREATED: "Draft created",
    AWAITING_AVAILABILITY: "Awaiting availability", READY_FOR_OWNER: "Ready for {owner}", OWNER_REVIEWING: "{owner} reviewing",
    APPROVED: "Approved by {owner}", AWAITING_CALENDAR_ENTRY: "Awaiting entry in {cal}", ENTERED_IN_CALENDAR: "Entered into {cal}",
    CONFIRMED: "Confirmed in {cal}", INFORMATION_NEEDED: "Information needed", DECLINED: "Declined",
    CANCELLED: "Cancelled", COMPLETED: "Completed",
}
LABELS = {k: v.replace("{owner}", "the owner").replace("{cal}", "the calendar") for k, v in LABEL_TEMPLATES.items()}  # generic wording


def labels(con):
    """Status names in this client's wording (owner's name, their calendar)."""
    return {k: kb.fmt(con, v) for k, v in LABEL_TEMPLATES.items()}
PRE_DRAFT = (NEW_INQUIRY, QUALIFYING)
TERMINAL = (DECLINED, CANCELLED, COMPLETED)
OPEN = tuple(s for s in LABELS if s not in TERMINAL)
CUSTOMER_MAY_BE_TOLD_BOOKED = (CONFIRMED, COMPLETED)
# The AI may refresh fields only before the owner has started working the draft.
AI_EDITABLE = (NEW_INQUIRY, QUALIFYING, INFORMATION_NEEDED, DRAFT_CREATED, AWAITING_AVAILABILITY, READY_FOR_OWNER)
OWNER_EDITABLE = (NEW_INQUIRY, QUALIFYING, DRAFT_CREATED, AWAITING_AVAILABILITY, READY_FOR_OWNER, OWNER_REVIEWING,
                  INFORMATION_NEEDED, APPROVED, AWAITING_CALENDAR_ENTRY)

FIELD_NAMES = ("customer_name", "instagram_username", "contact", "service_key", "needs", "business",
               "preferred_date", "preferred_date_iso", "preferred_time", "alt_date", "alt_time", "notes", "requests",
               "photos", "price_override_text", "price_override_reason")
BOOKING_KEYS = ("customer_name", "service_key", "preferred_date", "preferred_time", "price_override_text")
OWNER = "owner"


class WorkflowError(Exception):
    pass


class Conflict(WorkflowError):
    pass


class Forbidden(WorkflowError):
    pass


def _row(con, appt_id):
    r = con.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if not r:
        raise WorkflowError("Appointment request not found.")
    return r


def fields_of(row):
    return store.j(row["fields"], {}) or {}


def booking_hash(fields):
    core = {k: (fields.get(k) or "") for k in BOOKING_KEYS}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def service_info(con, fields):
    s = kb.services(con, active_only=False).get(fields.get("service_key") or "")
    if not s:
        return {"label": None, "price": None, "duration": "Not provided yet", "consultation": False}
    price = fields.get("price_override_text") or kb.price_text(s)
    return {"label": s["label"], "price": price, "price_is_override": bool(fields.get("price_override_text")),
            "duration": f"{s['duration_min']} minutes" if s.get("duration_min") else kb.fmt(con, "Not provided by {owner} yet"),
            "duration_verified": bool(s.get("duration_min")), "consultation": bool(s.get("consultation")),
            "requires_photos": s.get("requires_photos")}


def required_missing(con, fields, kind="booking"):
    miss = []
    if not fields.get("customer_name"):
        miss.append("customer_name")
    svc = kb.services(con, active_only=False).get(fields.get("service_key") or "")
    if not svc:
        miss.append("service")
    if not fields.get("preferred_date"):
        miss.append("preferred_date")
    if not fields.get("preferred_time"):
        miss.append("preferred_time")
    if svc and svc.get("requires_photos") and not fields.get("photos"):
        miss.append("photos")
    return miss


MISSING_LABELS = {"customer_name": "Customer name", "service": "Which service",
                  "preferred_date": "Preferred date", "preferred_time": "Preferred time", "photos": "Screenshots or photos"}


def _event(con, appt_id, actor, action, frm, to, detail=None):
    con.execute("INSERT INTO appointment_events(appointment_id,at,actor,action,from_status,to_status,detail) VALUES(?,?,?,?,?,?,?)",
                (appt_id, store.now(), actor, action, frm, to, json.dumps(detail or {}, default=str)[:8000]))


def _require_owner(user, con=None):
    if not user or user.get("role") != OWNER:
        who = kb.profile(con)["owner"] if con is not None else "the account owner"
        raise Forbidden(f"Only {who} can do this. Bookings are finalized by {who} alone.")


def _check_version(row, expected_version):
    if expected_version is not None and int(expected_version) != int(row["version"]):
        raise Conflict("This request changed since you opened it (a new message or edit came in). Reload it and try again.")


def _save(con, appt_id, sets, actor, action, frm, to, detail=None):
    sets = dict(sets)
    sets["status"] = to
    sets["updated_at"] = store.now()
    cols = ", ".join(f"{k}=?" for k in sets) + ", version=version+1"
    con.execute(f"UPDATE appointments SET {cols} WHERE id=?", (*sets.values(), appt_id))
    _event(con, appt_id, actor, action, frm, to, detail)
    store.audit(con, actor, f"appointment.{action}", appt_id, {"from": frm, "to": to, **(detail or {})})
    con.commit()
    return get(con, appt_id)


# ------------------------------------------------------------------ creation and automatic moves (system / AI)
def open_for_conversation(con, conversation_id):
    r = con.execute(f"SELECT * FROM appointments WHERE conversation_id=? AND status IN ({','.join('?' * len(OPEN))}) "
                    "ORDER BY id DESC LIMIT 1", (conversation_id, *OPEN)).fetchone()
    return dict(r) if r else None


def create(con, conversation_id, customer_id, kind="booking", fields=None, is_test=0):
    f = {k: v for k, v in (fields or {}).items() if k in FIELD_NAMES and v not in (None, "", [])}
    cur = con.execute("INSERT INTO appointments(conversation_id,customer_id,kind,status,fields,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?)",
                      (conversation_id, customer_id, kind, NEW_INQUIRY, json.dumps(f), store.now(), store.now(), is_test))
    aid = cur.lastrowid
    _event(con, aid, "ai", "created", None, NEW_INQUIRY, {"kind": kind})
    con.commit()
    return aid


def ai_update(con, appt_id, new_fields, *, summary=None, confidence=None, questions=None, escalation_reason=None, actor="ai"):
    """Merge what the AI extracted. Never touches a draft the owner is reviewing or has approved; then runs auto-advance."""
    row = _row(con, appt_id)
    clean = {k: v for k, v in (new_fields or {}).items() if k in FIELD_NAMES and k not in ("price_override_text", "price_override_reason")
             and v not in (None, "", [])}
    if row["status"] not in AI_EDITABLE:
        if clean:
            add_note(con, appt_id, "ai", "Customer sent new details after you started on this request: " +
                     "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in clean.items() if k != "photos"))
            con.execute("UPDATE appointments SET version=version+1, updated_at=? WHERE id=?", (store.now(), appt_id))
            con.commit()
        return get(con, appt_id)
    f = fields_of(row)
    before = dict(f)
    if "photos" in clean:
        clean["photos"] = list(dict.fromkeys((f.get("photos") or []) + list(clean["photos"])))[:20]
    f.update(clean)
    sets = {"fields": json.dumps(f)}
    if summary is not None:
        sets["summary"] = str(summary)[:2000]
    if confidence is not None:
        sets["confidence"] = max(0.0, min(1.0, float(confidence)))
    if questions is not None:
        sets["questions"] = json.dumps([str(q)[:300] for q in questions][:10])
    if escalation_reason:
        sets["escalation_reason"] = str(escalation_reason)[:300]
    status = row["status"]
    slot_changed = (before.get("preferred_date"), before.get("preferred_time")) != (f.get("preferred_date"), f.get("preferred_time"))
    if row["availability"] == "verified" and slot_changed:
        sets["availability"] = "awaiting"
        sets["verified_slot"] = None
        if status == READY_FOR_OWNER:
            status = AWAITING_AVAILABILITY
    missing = required_missing(con, f, row["kind"])
    sets["missing"] = json.dumps(missing)
    nxt = _auto_next(status, missing, bool(clean))
    if nxt == DRAFT_CREATED or (status == INFORMATION_NEEDED and nxt == AWAITING_AVAILABILITY):
        sets["draft_created_at"] = row["draft_created_at"] or store.now()
    if nxt != row["status"] or sets.get("availability") == "awaiting":
        out = _save(con, appt_id, sets, actor, "auto_update", row["status"], nxt, {"fields": sorted(clean)})
    else:
        sets["updated_at"] = store.now()
        cols = ", ".join(f"{k}=?" for k in sets) + ", version=version+1"
        con.execute(f"UPDATE appointments SET {cols} WHERE id=?", (*sets.values(), appt_id))
        _event(con, appt_id, actor, "fields_updated", row["status"], row["status"], {"fields": sorted(clean)})
        con.commit()
        out = get(con, appt_id)
    # a draft never sits at DRAFT_CREATED: with no authorized calendar read, it goes straight to the owner's check
    if out["status"] == DRAFT_CREATED:
        out = _save(con, appt_id, {}, "system", "awaiting_availability", DRAFT_CREATED, AWAITING_AVAILABILITY,
                    {"why": kb.fmt(con, "No calendar integration: {owner} verifies availability in {cal}.")})
    return out


def _auto_next(status, missing, got_fields):
    if status == NEW_INQUIRY:
        return DRAFT_CREATED if not missing else (QUALIFYING if got_fields else NEW_INQUIRY)
    if status == QUALIFYING:
        return DRAFT_CREATED if not missing else QUALIFYING
    if status == INFORMATION_NEEDED:
        return AWAITING_AVAILABILITY if got_fields and not missing else INFORMATION_NEEDED
    return status


def customer_replied(con, appt_id):
    """Customer answered while the request was waiting on them: back to the owner's queue (fields may already be complete)."""
    row = _row(con, appt_id)
    if row["status"] == INFORMATION_NEEDED and not store.j(row["missing"], []):
        return _save(con, appt_id, {}, "system", "customer_replied", INFORMATION_NEEDED, AWAITING_AVAILABILITY)
    return get(con, appt_id)


# ------------------------------------------------------------------ owner actions
def mark_reviewing(con, appt_id, user):
    row = _row(con, appt_id)
    if user and user.get("role") == OWNER and row["status"] == READY_FOR_OWNER:
        return _save(con, appt_id, {}, user["username"], "review_started", READY_FOR_OWNER, OWNER_REVIEWING)
    return get(con, appt_id)


def edit(con, appt_id, user, changes, expected_version=None):
    """The owner edits details. Price overrides (pricing exceptions) are owner-only and need a reason."""
    if not user:
        raise Forbidden("Sign in first.")
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] not in OWNER_EDITABLE:
        raise WorkflowError(f"Details can't be edited once the request is {LABELS[row['status']].lower()}.")
    changes = {k: v for k, v in (changes or {}).items() if k in FIELD_NAMES}
    if ("price_override_text" in changes or "price_override_reason" in changes) and user.get("role") != OWNER:
        raise Forbidden(kb.fmt(con, "Only {owner} can make a pricing exception."))
    if changes.get("price_override_text") and not str(changes.get("price_override_reason") or fields_of(row).get("price_override_reason") or "").strip():
        raise WorkflowError("Add a short reason for the pricing exception.")
    if "service_key" in changes and changes["service_key"] and changes["service_key"] not in kb.services(con, active_only=False):
        raise WorkflowError("Pick a service from the knowledge base.")
    f = fields_of(row)
    before_hash = booking_hash(f)
    for k, v in changes.items():
        if v in (None, "", []):
            f.pop(k, None)
        else:
            f[k] = v if k == "photos" else str(v)[:1000]
    sets = {"fields": json.dumps(f), "missing": json.dumps(required_missing(con, f, row["kind"]))}
    status = row["status"]
    detail = {"changed": sorted(changes)}
    slot = (f.get("preferred_date"), f.get("preferred_time"))
    if row["availability"] == "verified" and list(slot) != (store.j(row["verified_slot"], None) or [None, None]):
        sets["availability"] = "awaiting"
        sets["verified_slot"] = None
        detail["availability_reset"] = True
    if status in (APPROVED, AWAITING_CALENDAR_ENTRY) and booking_hash(f) != before_hash:
        sets.update(approved_hash=None, approved_by=None, approved_at=None)
        detail["approval_voided"] = True
        status = READY_FOR_OWNER
    if sets.get("availability") == "awaiting" and status in (READY_FOR_OWNER, OWNER_REVIEWING):
        status = AWAITING_AVAILABILITY
    if status == READY_FOR_OWNER and user.get("role") == OWNER:
        status = OWNER_REVIEWING
    return _save(con, appt_id, sets, user["username"], "edited", row["status"], status, detail)


def verify_availability(con, appt_id, user, date, time, expected_version=None, note=""):
    """The owner checked their own calendar and the slot is open. JARVIS never infers availability."""
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] not in (DRAFT_CREATED, AWAITING_AVAILABILITY, READY_FOR_OWNER, OWNER_REVIEWING):
        raise WorkflowError(f"Availability can't be verified while the request is {LABELS[row['status']].lower()}.")
    date, time = str(date or "").strip(), str(time or "").strip()
    if not date or not time:
        raise WorkflowError(kb.fmt(con, "Enter the date and time you checked in {cal}."))
    f = fields_of(row)
    f["preferred_date"], f["preferred_time"] = date[:100], time[:100]
    to = OWNER_REVIEWING if row["status"] == OWNER_REVIEWING else READY_FOR_OWNER
    return _save(con, appt_id, {"fields": json.dumps(f), "availability": "verified", "verified_slot": json.dumps([date[:100], time[:100]]),
                                "missing": json.dumps(required_missing(con, f, row["kind"]))},
                 user["username"], "availability_verified", row["status"], to, {"slot": [date, time], "note": note[:300]})


def approve(con, appt_id, user, expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] not in (READY_FOR_OWNER, OWNER_REVIEWING):
        raise WorkflowError(f"Can't approve a request that is {LABELS[row['status']].lower()}.")
    if row["availability"] != "verified":
        raise WorkflowError(kb.fmt(con, "Check {cal} and mark availability verified first."))
    f = fields_of(row)
    miss = required_missing(con, f, row["kind"])
    if miss:
        raise WorkflowError("Still missing: " + ", ".join(MISSING_LABELS.get(m, m) for m in miss) + ".")
    if [f.get("preferred_date"), f.get("preferred_time")] != store.j(row["verified_slot"], None):
        raise WorkflowError("The date or time changed after you verified availability. Verify it again.")
    h = booking_hash(f)
    out = _save(con, appt_id, {"approved_hash": h, "approved_by": user["username"], "approved_at": store.now()},
                user["username"], "approved", row["status"], APPROVED, {"hash": h[:12]})
    if store.get(con, "calendar_mode", "manual") == "manual":
        out = _save(con, appt_id, {}, "system", "awaiting_calendar_entry", APPROVED, AWAITING_CALENDAR_ENTRY,
                    {"why": kb.fmt(con, "Entry in {cal} is manual: no authorized calendar integration.")})
    return out


def mark_entered(con, appt_id, user, attest, calendar_ref="", expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] != AWAITING_CALENDAR_ENTRY:
        raise WorkflowError(kb.fmt(con, "Only approved requests waiting for entry in {cal} can be marked entered."))
    if not row["approved_hash"] or row["approved_hash"] != booking_hash(fields_of(row)):
        raise WorkflowError("The approval doesn't match the current details. Approve it again.")
    if attest is not True:
        raise WorkflowError(kb.fmt(con, "Confirm that you entered this in {cal}."))
    return _save(con, appt_id, {"calendar_entered_at": store.now(), "calendar_entered_by": user["username"], "calendar_ref": str(calendar_ref or "")[:120]},
                 user["username"], "entered_in_calendar", AWAITING_CALENDAR_ENTRY, ENTERED_IN_CALENDAR, {"calendar_ref": calendar_ref})


def mark_confirmed(con, appt_id, user, attest, expected_version=None):
    """The owner sees the booking on their calendar. Only now may the customer be told they're booked."""
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] != ENTERED_IN_CALENDAR or not row["calendar_entered_at"]:
        raise WorkflowError(kb.fmt(con, "Record the entry in {cal} first."))
    if row["approved_hash"] != booking_hash(fields_of(row)):
        raise WorkflowError("The approval doesn't match the current details.")
    if attest is not True:
        raise WorkflowError(kb.fmt(con, "Confirm you can see it in {cal}."))
    return _save(con, appt_id, {"confirmed_at": store.now(), "confirmed_by": user["username"]},
                 user["username"], "confirmed", ENTERED_IN_CALENDAR, CONFIRMED)


def request_info(con, appt_id, user, question, expected_version=None):
    if not user:
        raise Forbidden("Sign in first.")
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] not in (NEW_INQUIRY, QUALIFYING, DRAFT_CREATED, AWAITING_AVAILABILITY, READY_FOR_OWNER, OWNER_REVIEWING, APPROVED, AWAITING_CALENDAR_ENTRY):
        raise WorkflowError(f"Can't request information while the request is {LABELS[row['status']].lower()}.")
    q = str(question or "").strip()
    if not q:
        raise WorkflowError("Write the question for the customer.")
    qs = (store.j(row["questions"], []) or []) + [q[:300]]
    return _save(con, appt_id, {"questions": json.dumps(qs[-10:]), "approved_hash": None, "approved_by": None, "approved_at": None},
                 user["username"], "information_requested", row["status"], INFORMATION_NEEDED, {"question": q[:300]})


def decline(con, appt_id, user, reason, expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] in TERMINAL or row["status"] in (ENTERED_IN_CALENDAR, CONFIRMED):
        raise WorkflowError("This request can't be declined now" + (" — cancel it instead." if row["status"] in (ENTERED_IN_CALENDAR, CONFIRMED) else "."))
    return _save(con, appt_id, {"decline_reason": str(reason or "")[:500], "approved_hash": None}, user["username"], "declined",
                 row["status"], DECLINED, {"reason": str(reason or "")[:300]})


def cancel(con, appt_id, user, reason, expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] in TERMINAL:
        raise WorkflowError("This request is already closed.")
    was_on_calendar = row["status"] in (ENTERED_IN_CALENDAR, CONFIRMED)
    return _save(con, appt_id, {"decline_reason": str(reason or "")[:500]}, user["username"], "cancelled", row["status"], CANCELLED,
                 {"reason": str(reason or "")[:300], "remove_from_calendar": was_on_calendar})


def complete(con, appt_id, user, expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] != CONFIRMED:
        raise WorkflowError("Only confirmed appointments can be marked completed.")
    return _save(con, appt_id, {}, user["username"], "completed", CONFIRMED, COMPLETED)


def return_to_pending(con, appt_id, user, expected_version=None):
    _require_owner(user, con)
    row = _row(con, appt_id)
    _check_version(row, expected_version)
    if row["status"] not in (READY_FOR_OWNER, OWNER_REVIEWING, APPROVED, AWAITING_CALENDAR_ENTRY, INFORMATION_NEEDED, DECLINED):
        raise WorkflowError(f"A request that is {LABELS[row['status']].lower()} can't go back to pending" +
                            (kb.fmt(con, " — cancel it in {cal} and here instead.") if row["status"] in (ENTERED_IN_CALENDAR, CONFIRMED) else "."))
    return _save(con, appt_id, {"availability": "awaiting", "verified_slot": None, "approved_hash": None, "approved_by": None,
                                "approved_at": None, "decline_reason": None}, user["username"], "returned_to_pending",
                 row["status"], AWAITING_AVAILABILITY)


def add_note(con, appt_id, author, text):
    t = str(text or "").strip()
    if not t:
        raise WorkflowError("Note is empty.")
    _row(con, appt_id)
    con.execute("INSERT INTO appointment_notes(appointment_id,at,author,text) VALUES(?,?,?,?)", (appt_id, store.now(), author, t[:2000]))
    store.audit(con, author, "appointment.note", appt_id)
    con.commit()


# ------------------------------------------------------------------ reading
def may_tell_customer_booked(row):
    return bool(row and row["status"] in CUSTOMER_MAY_BE_TOLD_BOOKED and row["calendar_entered_at"] and row["confirmed_at"])


def get(con, appt_id):
    r = _row(con, appt_id)
    d = dict(r)
    d["fields"] = fields_of(r)
    d["missing"] = store.j(r["missing"], []) or []
    d["questions"] = store.j(r["questions"], []) or []
    d["verified_slot"] = store.j(r["verified_slot"], None)
    d["status_label"] = labels(con)[r["status"]]
    d["service"] = service_info(con, d["fields"])
    d["availability_label"] = {"awaiting": kb.fmt(con, "Awaiting {owner_s} verification"),
                               "verified": kb.fmt(con, "Verified by {owner} in {cal}")}.get(r["availability"], r["availability"])
    d["approval_valid"] = bool(r["approved_hash"] and r["approved_hash"] == booking_hash(d["fields"]))
    d["customer_may_be_told_booked"] = may_tell_customer_booked(r)
    return d


def detail(con, appt_id):
    d = get(con, appt_id)
    d["events"] = [dict(e) | {"detail": store.j(e["detail"], {})} for e in con.execute(
        "SELECT at,actor,action,from_status,to_status,detail FROM appointment_events WHERE appointment_id=? ORDER BY id", (appt_id,))]
    d["notes"] = [dict(n) for n in con.execute("SELECT at,author,text FROM appointment_notes WHERE appointment_id=? ORDER BY id", (appt_id,))]
    return d


QUEUE_GROUPS = [
    ("needs_availability", "Check availability in {cal}", (DRAFT_CREATED, AWAITING_AVAILABILITY)),
    ("ready", "Ready for your approval", (READY_FOR_OWNER, OWNER_REVIEWING)),
    ("info_needed", "Waiting on the customer", (INFORMATION_NEEDED,)),
    ("enter_calendar", "Approved — enter into {cal}", (APPROVED, AWAITING_CALENDAR_ENTRY)),
    ("confirm_calendar", "Entered — confirm in {cal}", (ENTERED_IN_CALENDAR,)),
    ("qualifying", "AI still collecting details", (NEW_INQUIRY, QUALIFYING)),
]


def queue(con, include_test=True):
    rows = con.execute(f"SELECT id FROM appointments WHERE status IN ({','.join('?' * len(OPEN))}) {'' if include_test else 'AND is_test=0'} "
                       "ORDER BY COALESCE(draft_created_at, created_at)", OPEN).fetchall()
    items = [get(con, r["id"]) for r in rows]
    cust = {r["id"]: dict(r) for r in con.execute("SELECT id,username,name,igsid FROM customers")}
    for it in items:
        it["customer"] = cust.get(it["customer_id"], {})
    groups = []
    for key, label, sts in QUEUE_GROUPS:
        groups.append({"key": key, "label": kb.fmt(con, label), "items": [i for i in items if i["status"] in sts]})
    return {"groups": groups, "attention": sum(len(g["items"]) for g in groups if g["key"] not in ("qualifying", "info_needed"))}
