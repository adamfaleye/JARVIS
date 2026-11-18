"""Metrics from real records only. Test (simulator) conversations are excluded unless asked for.
"Verified on the calendar" counts only requests the owner marked entered AND confirmed. Time saved is labeled an estimate."""
from datetime import datetime, timedelta

from . import store, workflow


def _since(days):
    return (datetime.now() - timedelta(days=days)).astimezone().isoformat(timespec="seconds")


def report(con, days=30, include_test=False):
    since = _since(days)
    t = "" if include_test else " AND c.is_test=0"
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
    inbound = q(f"SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.direction='in' AND m.created_at>=?{t}", since)
    convs = q(f"SELECT COUNT(DISTINCT m.conversation_id) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.direction='in' AND m.created_at>=?{t}", since)
    booking_convs = q(f"SELECT COUNT(DISTINCT a.conversation_id) FROM appointments a JOIN conversations c ON c.id=a.conversation_id WHERE a.created_at>=?{t}", since)
    sent = lambda authors: q(f"SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.direction='out' AND m.status='sent' "  # noqa: E731
                             f"AND m.author IN ({','.join('?' * len(authors))}) AND m.sent_at>=?{t}", *authors, since)
    ai_sent = sent(("ai", "ai_reviewed"))
    owner_sent = sent(("owner",))
    corrected = q(f"SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.author='owner' AND m.ai_original IS NOT NULL "
                  f"AND m.ai_original != m.text AND m.created_at>=?{t}", since)
    blocked = q(f"SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.status='blocked' AND m.author='ai' AND m.created_at>=?{t}", since)
    ai_convs = q(f"SELECT COUNT(DISTINCT m.conversation_id) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.direction='out' "
                 f"AND m.status='sent' AND m.author IN ('ai','ai_reviewed') AND m.sent_at>=?{t}", since)
    human_convs = q(f"SELECT COUNT(DISTINCT m.conversation_id) FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.direction='out' "
                    f"AND m.status='sent' AND m.author='owner' AND m.sent_at>=?{t}", since)
    esc = q(f"SELECT COUNT(*) FROM audit a JOIN conversations c ON CAST(a.target AS INTEGER)=c.id WHERE a.action='conversation.escalated' AND a.at>=?{t}", since)
    ev = lambda action: q(f"SELECT COUNT(DISTINCT e.appointment_id) FROM appointment_events e JOIN appointments a ON a.id=e.appointment_id "  # noqa: E731
                          f"JOIN conversations c ON c.id=a.conversation_id WHERE e.action=? AND e.at>=?{t}", action, since)
    drafts = ev("awaiting_availability")
    approved = ev("approved")
    declined = ev("declined")
    entered = ev("entered_in_calendar")
    verified = q(f"SELECT COUNT(*) FROM appointments a JOIN conversations c ON c.id=a.conversation_id WHERE a.confirmed_at IS NOT NULL "
                 f"AND a.calendar_entered_at IS NOT NULL AND a.confirmed_at>=?{t}", since)
    # first-response time: each inbound message to the next real sent reply
    rt = []
    for r in con.execute(f"SELECT m.id, m.conversation_id, m.created_at FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                         f"WHERE m.direction='in' AND m.created_at>=?{t}", (since,)):
        nxt = con.execute("SELECT sent_at FROM messages WHERE conversation_id=? AND direction='out' AND status='sent' AND id>? ORDER BY id LIMIT 1",
                          (r["conversation_id"], r["id"])).fetchone()
        a, b = store.parse_ts(r["created_at"]), store.parse_ts(nxt["sent_at"]) if nxt else None
        if a and b and b >= a:
            rt.append((b - a).total_seconds())
    to_approval = []
    for r in con.execute(f"SELECT a.created_at, a.approved_at FROM appointments a JOIN conversations c ON c.id=a.conversation_id "
                         f"WHERE a.approved_at IS NOT NULL AND a.approved_at>=?{t}", (since,)):
        x, y = store.parse_ts(r["created_at"]), store.parse_ts(r["approved_at"])
        if x and y:
            to_approval.append((y - x).total_seconds())
    try:
        mpr = float(store.get(con, "minutes_per_reply") or 2)
    except ValueError:
        mpr = 2.0
    return {
        "days": days, "include_test": include_test,
        "incoming_dms": inbound, "conversations": convs, "booking_inquiries": booking_convs,
        "ai_replies_sent": ai_sent, "owner_replies_sent": owner_sent,
        "ai_handled_conversations": ai_convs, "human_handled_conversations": human_convs,
        "drafts_generated": drafts, "drafts_approved": approved, "drafts_declined": declined,
        "entered_in_calendar": entered, "verified_in_calendar": verified,
        "approval_rate": round(approved / drafts, 3) if drafts else None,
        "decline_rate": round(declined / drafts, 3) if drafts else None,
        "avg_response_seconds": round(sum(rt) / len(rt)) if rt else None, "responses_measured": len(rt),
        "avg_inquiry_to_approval_hours": round(sum(to_approval) / len(to_approval) / 3600, 1) if to_approval else None,
        "ai_corrections": corrected, "ai_replies_blocked_by_guards": blocked, "escalations": esc,
        "human_intervention_rate": round(human_convs / convs, 3) if convs else None,
        "estimated_minutes_saved": round(ai_sent * mpr, 1),
        "estimate_note": f"Estimate: {ai_sent} AI replies actually sent × {mpr:g} min each (your setting). Not a measured figure.",
        "baseline": store.j(store.get(con, "baseline"), None),
        "open_queue": workflow.queue(con, include_test)["attention"],
    }
