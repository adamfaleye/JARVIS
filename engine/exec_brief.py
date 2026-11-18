"""Executive morning brief (JARVIS 2.0): both businesses, workforce and infrastructure in one read.

Read model: exec_brief() only reads. Two small tables give it memory, written by the work loop (run.py), never by the brief:
  hq_incidents        problems the loop saw (Gmail quota/sign-in, scrape errors, loop crashes, stuck tasks, repeated
                      failures, loop downtime), with first/last seen, count and when the loop verified recovery.
  hq_daily_snapshot   once a day, the levels that can't be rebuilt from timestamps later (backlog sizes, pipeline).

Rules: numbers come from records only. A metric with no source is listed under not_tracked, never estimated.
Activity windows are calendar days in the PC's local time (America/New_York), not a rolling 24-29 hours.
Each section is computed on its own; if one fails, the brief still returns the rest and lists the failure.
"""
import json
from datetime import date, datetime, timedelta

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_incidents(
  id INTEGER PRIMARY KEY, key TEXT, area TEXT, message TEXT, first_seen TEXT, last_seen TEXT,
  count INTEGER DEFAULT 1, resolved_at TEXT, recovery TEXT);
CREATE INDEX IF NOT EXISTS hq_incidents_open ON hq_incidents(key, resolved_at);
CREATE TABLE IF NOT EXISTS hq_daily_snapshot(day TEXT PRIMARY KEY, at TEXT, data TEXT);
"""

# The AI Sidekicks workforce, in the order Mola reads it (ids from engine/org.py).
WORKFORCE = [("architect", "AI Solutions Architect"), ("marketing", "Marketing Director"), ("qa", "QA Engineer"),
             ("builder", "AI Builder/Developer"), ("pm", "Project Manager"), ("account_mgr", "Account Manager"),
             ("support", "Support Specialist"), ("ae", "Account Executive"), ("sdr", "SDR/Appointment Setter")]
LOOP_STALE_MIN = 10  # matches morning_brief: no heartbeat for 10 minutes = loop not running
STALE_BLOCK_H = 48  # a task blocked longer than this with no retry is reported as old history, not as a live blocker
RECOVERY = {
    "loop_down": "Run Stop-JARVIS, then Start-JARVIS on the PC (keep the PC awake while JARVIS should work).",
    "loop_error": "Check the JARVIS Engine window for the error; restart with Stop-JARVIS / Start-JARVIS if it repeats.",
    "gmail_quota": "Gmail API per-minute quota hit. Sending resumes on its own once the minute passes; if it repeats, lower the campaign's daily limit.",
    "gmail_auth": "Double-click Connect-Gmail.bat in the JARVIS folder to sign in again, then restart JARVIS.",
    "gmail": "Check the engine window; retry after the error clears.",
    "scrape": "The county site may be down or changed. The loop retries in 30 minutes; if it keeps failing, check the adapter.",
    "source": "Check that public-record source in The Hunter; it retries on its own schedule.",
    "stuck": "Restart JARVIS; stale leases are recovered on start.",
    "repeat_fail": "Open the failed tasks in the JARVIS app, read the error, fix the cause, then retry.",
}


def ensure(con):
    con.executescript(SCHEMA)


def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _age_min(ts, now=None):
    try:
        return round(((now or _now()) - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def classify_gmail(err):
    e = str(err or "").lower()
    if not e:
        return None
    if "quota" in e or "rate limit" in e or "429" in e or ("403" in e and "exceeded" in e):
        return "gmail_quota"
    if "sign-in expired" in e or "invalid_grant" in e or "401" in e or "expired" in e or "revoked" in e:
        return "gmail_auth"
    return "gmail"


# ---------------------------------------------------------------- incidents (written by the loop only)
def record(con, key, area, message, at=None):
    """Open an incident for `key`, or bump the open one. Returns its id."""
    at = at or _iso(_now())
    r = con.execute("SELECT id FROM hq_incidents WHERE key=? AND resolved_at IS NULL", (key,)).fetchone()
    if r:
        con.execute("UPDATE hq_incidents SET last_seen=?, count=count+1, message=? WHERE id=?", (at, str(message)[:300], r[0]))
        return r[0]
    cur = con.execute("INSERT INTO hq_incidents(key,area,message,first_seen,last_seen,count,recovery) VALUES(?,?,?,?,?,1,?)",
                      (key, area, str(message)[:300], at, at, RECOVERY.get(area, "")))
    return cur.lastrowid


def resolve(con, key, at=None):
    """Close the open incident for `key` (the loop saw the condition clear)."""
    con.execute("UPDATE hq_incidents SET resolved_at=? WHERE key=? AND resolved_at IS NULL", (at or _iso(_now()), key))


def on_loop_start(con, at=None):
    """Called once when the loop starts: a gap in the heartbeat is recorded as downtime (already over)."""
    ensure(con)
    at = at or _now()
    last = db.kv_get(con, "hq_last_tick") or ""
    age = _age_min(last, at)
    if age is not None and age >= LOOP_STALE_MIN:
        con.execute("INSERT INTO hq_incidents(key,area,message,first_seen,last_seen,count,resolved_at,recovery) VALUES(?,?,?,?,?,1,?,?)",
                    ("loop_down", "loop_down", f"Work loop was not running for {_dur(age)}", last[:19], _iso(at), _iso(at), RECOVERY["loop_down"]))
        con.commit()


def watch(con, cfg=None, loop_error=None, at=None):
    """One pass per loop tick: open, bump or resolve incidents from the engine's own state. Never raises."""
    ensure(con)
    at = at or _now()
    ts = _iso(at)
    try:
        if loop_error:
            record(con, "loop_error", "loop_error", loop_error, ts)
        else:
            resolve(con, "loop_error", ts)
        err = db.kv_get(con, "di_gmail_error") or ""
        kind = classify_gmail(err)
        for k in ("gmail_quota", "gmail_auth", "gmail"):
            if k == kind:
                record(con, k, k, err, ts)
            else:
                resolve(con, k, ts)
        # Gmail send failures land in hq_events, not in di_gmail_error: a quota error on a send opens the same incident
        recent = con.execute("SELECT message FROM hq_events WHERE type='send_failed' AND at>=? ORDER BY id DESC LIMIT 1",
                             (_iso(at - timedelta(minutes=2)),)).fetchone()
        if recent and classify_gmail(recent[0]):
            record(con, classify_gmail(recent[0]), classify_gmail(recent[0]), recent[0], ts)
        # county scrapes: latest run per target
        for r in con.execute("SELECT target, error FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE kind='scrape' GROUP BY target)"):
            (record(con, f"scrape:{r[0]}", "scrape", f"{r[0]}: {r[1]}", ts) if r[1] else resolve(con, f"scrape:{r[0]}", ts))
        try:
            for r in con.execute("SELECT id, last_error FROM hunter_sources WHERE enabled=1"):
                (record(con, f"source:{r[0]}", "source", f"{r[0]}: {r[1]}", ts) if r[1] else resolve(con, f"source:{r[0]}", ts))
        except Exception:  # noqa: BLE001  (Hunter tables may not exist on a fresh install)
            pass
        stuck = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?", (ts,)).fetchone()[0]
        (record(con, "stuck_tasks", "stuck", f"{stuck} running task(s) past their lease", ts) if stuck else resolve(con, "stuck_tasks", ts))
        since = _iso(at - timedelta(hours=24))
        bad = {r[0]: r[1] for r in con.execute("SELECT kind, COUNT(*) FROM hq_tasks WHERE status IN ('failed','blocked') AND updated>=? GROUP BY kind", (since,))}
        open_rf = [r[0] for r in con.execute("SELECT key FROM hq_incidents WHERE key LIKE 'repeat_fail:%' AND resolved_at IS NULL")]
        for kind_, n in bad.items():
            if n >= 3:
                record(con, f"repeat_fail:{kind_}", "repeat_fail", f"{n} {kind_} task(s) failed or blocked in 24 h", ts)
        for k in open_rf:
            if bad.get(k.split(":", 1)[1], 0) < 3:
                resolve(con, k, ts)
        con.commit()
    except Exception as e:  # noqa: BLE001  the watcher must never stop the loop
        try:
            print(f"[{at:%H:%M}] incident watch: {e}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- daily snapshot (written by the loop, once a day)
def levels(con):
    """Point-in-time levels: these can't be rebuilt from timestamps later, so the loop saves them daily."""
    from . import crm
    out = {}
    try:
        out["county_leads_waiting"] = con.execute("SELECT COUNT(*) FROM leads WHERE status='new' AND " + db.UPCOMING).fetchone()[0]
    except Exception:  # noqa: BLE001
        pass
    try:
        q = crm.queue(con)
        out["leads_not_contacted"] = sum(1 for d in q if not d.get("nextFollowUp"))
        out["follow_ups_due"] = sum(1 for d in q if d.get("nextFollowUp"))
        out["deals_by_stage"] = crm.overview(con)["countsByStage"]
    except Exception:  # noqa: BLE001
        pass
    try:
        out["hunter_by_tier"] = {r[0]: r[1] for r in con.execute(
            "SELECT s.tier, COUNT(*) FROM lead_scores s JOIN hunter_leads h ON h.property_id=s.property_id WHERE h.status='new' GROUP BY s.tier")}
    except Exception:  # noqa: BLE001
        pass
    out["tasks_by_status"] = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM hq_tasks GROUP BY status")}
    out["approvals_waiting"] = out["tasks_by_status"].get("awaiting_approval", 0)
    out["campaign_items"] = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM hq_campaign_items GROUP BY status")}
    return out


def snapshot(con, at=None):
    """Save today's levels once (first loop tick of the day). Returns True if it wrote one."""
    ensure(con)
    at = at or _now()
    day = at.date().isoformat()
    if con.execute("SELECT 1 FROM hq_daily_snapshot WHERE day=?", (day,)).fetchone():
        return False
    con.execute("INSERT INTO hq_daily_snapshot(day,at,data) VALUES(?,?,?)", (day, _iso(at), json.dumps(levels(con))))
    con.commit()
    return True


# ---------------------------------------------------------------- activity in a window (from timestamps)
def _count(con, sql, *a):
    return con.execute(sql, a).fetchone()[0] or 0


def activity(con, start, end):
    """Completed work between start (inclusive) and end (exclusive), ISO strings in local time. Test sends excluded."""
    w = (start, end)
    task = lambda kind, status="completed", col="finished": _count(  # noqa: E731
        con, f"SELECT COUNT(*) FROM hq_tasks WHERE kind=? AND status=? AND COALESCE(test,0)=0 AND {col}>=? AND {col}<?", kind, status, *w)
    reps = {r[0] or "unread": r[1] for r in con.execute("SELECT class, COUNT(*) FROM hq_email_replies WHERE at>=? AND at<? GROUP BY class", w)}
    d = {
        "prospect_searches_run": task("sdr.find_prospects"),
        "contact_lookups_done": task("sdr.find_contact"),
        "emails_drafted": task("sdr.campaign_email") + task("sdr.outreach_draft"),
        "drafts_blocked": task("sdr.campaign_email", "blocked", "updated") + task("sdr.outreach_draft", "blocked", "updated"),
        "emails_sent": task("sdr.send_email"),
        "send_failures": _count(con, "SELECT COUNT(*) FROM hq_events WHERE type='send_failed' AND at>=? AND at<?", *w),
        "replies": sum(reps.values()),
        "replies_positive": reps.get("interested", 0) + reps.get("meeting", 0),
        "replies_negative": reps.get("not_interested", 0) + reps.get("not_now", 0) + reps.get("opt_out", 0),
        "replies_other": sum(v for k, v in reps.items() if k not in ("interested", "meeting", "not_interested", "not_now", "opt_out")),
        "meetings_booked": task("ae.book_meeting"),
    }
    m = {}
    try:
        m["county_leads_new"] = _count(con, "SELECT COUNT(*) FROM leads WHERE first_seen>=? AND first_seen<?", *w)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["clerk_records_new"] = {r[0]: r[1] for r in con.execute(
            "SELECT category, COUNT(*) FROM lead_events WHERE source_id LIKE '%clerk%' AND retrieved_at>=? AND retrieved_at<? GROUP BY category", w)}
        m["lead_scores_updated"] = _count(con, "SELECT COUNT(*) FROM lead_scores WHERE calculated_at>=? AND calculated_at<?", *w)
    except Exception:  # noqa: BLE001
        pass
    try:
        m["seller_texts_in"] = _count(con, "SELECT COUNT(*) FROM messages WHERE direction='in' AND at>=? AND at<?", *w)
        m["seller_texts_out"] = _count(con, "SELECT COUNT(*) FROM messages WHERE direction='out' AND at>=? AND at<?", *w)
    except Exception:  # noqa: BLE001
        pass
    m["scrapes_run"] = _count(con, "SELECT COUNT(*) FROM runs WHERE kind='scrape' AND finished>=? AND finished<?", *w)
    m["scrape_errors"] = _count(con, "SELECT COUNT(*) FROM runs WHERE kind='scrape' AND finished>=? AND finished<? AND error IS NOT NULL AND error!=''", *w)
    tasks = {r[0]: r[1] for r in con.execute(
        "SELECT status, COUNT(*) FROM hq_tasks WHERE COALESCE(test,0)=0 AND status IN ('completed','failed','blocked') AND updated>=? AND updated<? GROUP BY status", w)}
    done, failed = tasks.get("completed", 0), tasks.get("failed", 0) + tasks.get("blocked", 0)
    ai = con.execute("SELECT COALESCE(SUM(cost),0), COUNT(*) FROM hq_ai_usage WHERE at>=? AND at<?", w).fetchone()
    return {"from": start, "to": end, "demigod": d, "molabucks": m,
            "tasks": {"completed": done, "failed_or_blocked": failed, "failure_rate": round(100 * failed / (done + failed), 1) if done + failed else None},
            "ai_cost": round(ai[0], 4), "ai_calls": ai[1]}


def _rate(n, d):
    return round(100 * n / d, 1) if d else None


def windows(now=None):
    n = now or _now()
    t0 = datetime.combine(n.date(), datetime.min.time())
    m0 = t0.replace(day=1)
    pm_end = m0
    pm0 = (m0 - timedelta(days=1)).replace(day=1)
    # same number of elapsed days in the previous month, capped at that month's length
    pm_cut = min(pm0 + (n - m0), pm_end)
    return {
        "today": (t0, n), "yesterday": (t0 - timedelta(days=1), t0), "day_before": (t0 - timedelta(days=2), t0 - timedelta(days=1)),
        "last_7_days": (t0 - timedelta(days=7), t0), "prior_7_days": (t0 - timedelta(days=14), t0 - timedelta(days=7)),
        "month_to_date": (m0, n), "prev_month_same_days": (pm0, pm_cut),
    }


# ---------------------------------------------------------------- the brief
def _section(out, name, fn):
    try:
        out[name] = fn()
    except Exception as e:  # noqa: BLE001  one broken section never takes down the brief
        out[name] = {"unavailable": True, "error": f"{e.__class__.__name__}: {e}"[:200]}
        out.setdefault("sections_unavailable", []).append(name)


def _dur(minutes):
    if minutes is None:
        return "unknown"
    m = int(minutes)
    if m < 60:
        return f"{m} min"
    if m < 48 * 60:
        return f"{m // 60} h {m % 60} min"
    return f"{m // 1440} days {(m % 1440) // 60} h"


def exec_brief(con, cfg=None):
    """Read-only. Everything the executive morning email needs, with statuses decided by fixed rules."""
    from . import hq, crm, agents_ai
    cfg = cfg or {}
    ensure(con)  # creates the two tables if this engine hasn't run the loop since the upgrade (no rows written)
    now = _now()
    nowi = _iso(now)
    t = now.date().isoformat()
    out = {"generated_at": nowi, "date": t, "timezone": cfg.get("timezone", "America/New_York"),
           "window_note": "Activity counts use calendar days in local time (midnight to midnight), not a rolling window."}
    W = windows(now)
    hb = db.kv_get(con, "hq_last_tick") or ""
    hb_age = _age_min(hb, now)
    loop_ok = hb_age is not None and hb_age < LOOP_STALE_MIN

    # ---- activity and comparisons
    def act():
        a = {k: activity(con, _iso(s), _iso(e)) for k, (s, e) in W.items()}
        return a
    _section(out, "activity", act)

    # ---- infrastructure
    def infra():
        es = hq.engine_sender(con)
        paused = hq.paused(con)
        try:
            dbok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except Exception:  # noqa: BLE001
            dbok = False
        runs = [dict(r) for r in con.execute(
            "SELECT target, finished, found, new, error FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE kind='scrape' GROUP BY target) ORDER BY target")]
        for r in runs:
            r["age_hours"] = round((_age_min(r["finished"], now) or 0) / 60, 1) if r["finished"] else None
        try:
            sources = [dict(r) for r in con.execute("SELECT id, last_success, last_attempt, last_error FROM hunter_sources WHERE enabled=1 ORDER BY id")]
        except Exception:  # noqa: BLE001
            sources = []
        stuck = [dict(r) for r in con.execute("SELECT id, agent, title, lease_until FROM hq_tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?", (nowi,))]
        retry = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='queued' AND attempts>0").fetchone()[0]
        q = con.execute("SELECT COUNT(*) n, MIN(created) oldest FROM hq_tasks WHERE status='queued'").fetchone()
        inc_open = [dict(r) for r in con.execute("SELECT key, area, message, first_seen, last_seen, count, recovery FROM hq_incidents WHERE resolved_at IS NULL ORDER BY first_seen")]
        for i in inc_open:
            i["duration"] = _dur(_age_min(i["first_seen"], now))
        inc_recent = [dict(r) for r in con.execute(
            "SELECT key, area, message, first_seen, resolved_at, count FROM hq_incidents WHERE resolved_at IS NOT NULL AND resolved_at>=? ORDER BY resolved_at DESC LIMIT 10",
            (_iso(now - timedelta(days=7)),))]
        for i in inc_recent:
            try:
                i["duration"] = _dur((datetime.fromisoformat(i["resolved_at"][:19]) - datetime.fromisoformat(i["first_seen"][:19])).total_seconds() / 60)
            except ValueError:
                i["duration"] = "unknown"
        # Gmail failures from the event log (exists from before incident tracking), last 7 days
        gm = [dict(r) for r in con.execute("SELECT at, message FROM hq_events WHERE type='send_failed' AND at>=? ORDER BY id DESC",
                                           (_iso(now - timedelta(days=7)),))]
        quota = [g for g in gm if classify_gmail(g["message"]) == "gmail_quota"]
        try:
            bk = json.loads(db.kv_get(con, "backup_last") or "null")
        except ValueError:
            bk = None
        try:
            sysck = json.loads(db.kv_get(con, "system_check_last") or "null")
        except ValueError:
            sysck = None
        return {
            "work_loop": {"running": loop_ok, "last_tick": hb or None, "minutes_since_tick": hb_age, "paused": paused or None,
                          "expected_next": "within 30 seconds" if loop_ok else "only after JARVIS Engine is started again (Start-JARVIS)"},
            "scheduler": {"briefing_time": (cfg.get("hq") or {}).get("briefing_time", "08:00"), "scrape_hour": (cfg.get("scrape") or {}).get("schedule_hour", 6),
                          "last_scrape_day": db.kv_get(con, "last_scrape_day"), "clerk_day": db.kv_get(con, "clerk_day")},
            "database": {"ok": dbok},
            "email_sender": dict(es, error_kind=classify_gmail(es.get("last_error"))),
            "gmail_send_failures_7d": len(gm), "gmail_quota_errors_7d": len(quota),
            "gmail_last_failure": (dict(gm[0], kind=classify_gmail(gm[0]["message"]), message=gm[0]["message"][:200]) if gm else None),
            "gmail_quota_usage": "not tracked (the engine doesn't read Google's quota counters; only quota errors are seen)",
            "county_scrapers": runs, "record_sources": sources,
            "queue": {"queued": q["n"], "oldest_queued": q["oldest"], "retry_queue": retry,
                      "running": con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='running'").fetchone()[0]},
            "stuck_tasks": stuck,
            "incidents_open": inc_open, "incidents_resolved_7d": inc_recent,
            "backup": bk or {"status": "no verified backup recorded"},
            "system_check": sysck,
            "ai_spend": agents_ai.usage(con),
        }
    _section(out, "infrastructure", infra)

    # ---- workforce (the 9 AI Sidekicks employees)
    def workforce():
        ags = hq.agent_status(con)
        d0, d1, y0 = _iso(W["today"][0]), nowi, _iso(W["yesterday"][0])
        since24 = _iso(now - timedelta(hours=24))
        rows = []
        for aid, label in WORKFORCE:
            a = ags.get(aid)
            if not a:
                rows.append({"id": aid, "employee": label, "status": "offline", "reason": "not in the engine's roster"})
                continue
            c = lambda sql, *x: con.execute(sql, (aid, *x)).fetchone()[0]  # noqa: E731
            done_today = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status='completed' AND COALESCE(test,0)=0 AND finished>=? AND finished<?", d0, d1)
            done_yday = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status='completed' AND COALESCE(test,0)=0 AND finished>=? AND finished<?", y0, d0)
            failed24 = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status IN ('failed','blocked') AND updated>=?", since24)
            blocked_all = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status='blocked'")
            waiting = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status='waiting'")
            queued = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status='queued'")
            overdue = c("SELECT COUNT(*) FROM hq_tasks WHERE agent=? AND status IN ('queued','waiting','running','awaiting_approval') AND deadline IS NOT NULL AND deadline<?", nowi)
            nxt = con.execute("SELECT title, status, next_run_at FROM hq_tasks WHERE agent=? AND status IN ('queued','waiting') ORDER BY priority, COALESCE(next_run_at, created) LIMIT 1", (aid,)).fetchone()
            blk = con.execute("SELECT title, error, updated FROM hq_tasks WHERE agent=? AND status IN ('blocked','failed') AND updated>=? ORDER BY updated DESC LIMIT 1",
                              (aid, _iso(now - timedelta(hours=STALE_BLOCK_H)))).fetchone()
            run = a.get("running")
            if not a.get("operational"):
                st, why = "offline", "no task type with a working executor yet"
            elif not loop_ok:
                st, why = ("blocked", "work loop stopped with work queued") if (queued + waiting) else ("offline", "work loop stopped")
            elif run:
                st, why = "active", None
            elif a.get("approvals"):
                st, why = "waiting", "waiting on your approval"
            elif waiting:
                st, why = "waiting", "waiting on a dependency or its scheduled time"
            elif a.get("state") in ("failed", "blocked"):
                st, why = "blocked", (blk["error"] or "")[:160] if blk else None
            elif queued:
                st, why = "waiting", "queued, not started yet"
            else:
                st, why = "idle", ("monitoring: " + a["monitoring"]) if a.get("monitoring") else None
            if run and not loop_ok:
                why = "stale task lease with the loop stopped; recovered when JARVIS restarts"
            rows.append({"id": aid, "employee": label, "status": st, "reason": why, "operational": a.get("operational"),
                         "current": (run or {}).get("title"), "completed_today": done_today, "completed_yesterday": done_yday,
                         "in_progress": 1 if run else 0, "waiting_on_dependencies": waiting, "queued": queued, "awaiting_approval": a.get("approvals", 0),
                         "failed_or_blocked_24h": failed24, "blocked_total_incl_old": blocked_all, "overdue": overdue,
                         "blocker": ({"title": blk["title"], "error": (blk["error"] or "")[:200], "at": blk["updated"]} if blk else None),
                         "next": ({"title": nxt["title"], "status": nxt["status"], "at": nxt["next_run_at"]} if nxt else None),
                         "scorecard_7d": a.get("scorecard")})
        return rows
    _section(out, "workforce", workforce)

    # ---- AI Sidekicks
    def demigod():
        cs = hq.campaigns(con)["campaigns"]
        srch = hq.searches(con, 5)["searches"]
        reps = [dict(r) for r in con.execute("SELECT from_addr, at, class FROM hq_email_replies ORDER BY at DESC LIMIT 5")]
        appr = [dict(r) for r in con.execute("SELECT id, agent, title, created FROM hq_tasks WHERE status='awaiting_approval' AND division='demigod' ORDER BY created")]
        ready = sum(c["counts"].get("ready", 0) for c in cs)
        live = [c for c in cs if c["status"] in ("sending", "approved")]
        queued = sum(c["counts"].get("approved", 0) for c in live)
        limit = sum(int(c.get("daily_limit") or 0) for c in live)
        recent = _iso(now - timedelta(hours=STALE_BLOCK_H))
        blocked = [dict(r) for r in con.execute(
            "SELECT kind, COUNT(*) n, MAX(updated) last FROM hq_tasks WHERE division='demigod' AND status='blocked' AND updated>=? GROUP BY kind ORDER BY n DESC", (recent,))]
        stale_blocked = con.execute("SELECT COUNT(*), MIN(updated), MAX(updated) FROM hq_tasks WHERE division='demigod' AND status='blocked' AND updated<?", (recent,)).fetchone()
        for b in blocked:
            b["sample_error"] = (con.execute("SELECT error FROM hq_tasks WHERE kind=? AND status='blocked' ORDER BY updated DESC LIMIT 1", (b["kind"],)).fetchone()[0] or "")[:200]
        calls = []
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        for r in con.execute("SELECT status, params FROM hq_tasks WHERE kind='ae.book_meeting' AND status IN ('completed','awaiting_approval','queued','waiting')"):
            p = json.loads(r["params"] or "{}")
            st = str(p.get("start") or "")
            if st[:10] >= t:
                calls.append({"title": p.get("title"), "with": p.get("attendee"), "start": st, "booked": r["status"] == "completed"})
        calls.sort(key=lambda c: c["start"])
        tot = {"found": sum(s.get("found") or 0 for s in srch), "with_email": sum(s.get("with_email") or 0 for s in srch)}
        return {
            "prospecting": {"searches": [{"area": s["area"], "asked_for": s["count"], "found": s["found"], "with_email": s["with_email"],
                                          "parts_done": f"{s['parts_done']} of {s['parts']}",
                                          "email_lookups": f"{s['lookups_done']} of {s['lookups']}" if s.get("lookups") else "not run"} for s in srch],
                            "totals": tot,
                            "awaiting_research": con.execute("SELECT COUNT(*) FROM hq_tasks WHERE kind IN ('sdr.find_prospects','sdr.find_contact') AND status IN ('queued','waiting','running')").fetchone()[0]},
            "outreach": {"ready_for_review": ready, "approvals_waiting": len(appr), "approvals": [{"title": a["title"], "since": a["created"]} for a in appr[:5]],
                         "sending_today": min(queued, limit) if limit else queued, "follow_ups_due": len(hq.followups_due(con)["due"]),
                         "recent_replies": [{"from": r["from_addr"], "class": r["class"] or "not read yet", "at": r["at"]} for r in reps],
                         "upcoming_calls": calls[:5], "blocked_work": blocked,
                         "stale_blocked": {"count": stale_blocked[0], "oldest": stale_blocked[1], "newest": stale_blocked[2],
                                           "note": f"blocked more than {STALE_BLOCK_H} h ago and never retried; history to clean up, not new work"} if stale_blocked[0] else None},
            "campaigns": [{"name": c["name"], "status": c["status"], "created": c["created"], **{k: c["stats"].get(k) for k in
                           ("sent", "replied", "reply_rate", "interested", "wants_call", "not_now", "opted_out", "booked_calls")},
                           "positive_rate": _rate((c["stats"].get("interested") or 0) + (c["stats"].get("wants_call") or 0), c["stats"].get("sent") or 0),
                           "sample_note": "small sample, not conclusive" if (c["stats"].get("sent") or 0) < 100 else None,
                           "subject_line": hq.DI_SUBJECT} for c in cs[:5]],
            "funnel": {"prospect_to_meeting": _rate(con.execute("SELECT COUNT(*) FROM hq_tasks WHERE kind='ae.book_meeting' AND status='completed'").fetchone()[0], tot["found"]),
                       "meeting_to_client": None},
            "not_tracked": ["bounces and verified delivery", "email verification", "decision-makers identified (separately from contacts found)",
                            "discovery calls completed", "proposals delivered", "clients acquired", "revenue",
                            "marketing content (ideas, scripts, published, engagement, inbound)", "client delivery projects, QA failures, support tickets",
                            "results by industry or messaging variant (one subject line, one campaign so far)"],
        }
    _section(out, "demigod", demigod)

    # ---- MolaBucks Capital
    def molabucks():
        ov = crm.overview(con)
        q = crm.queue(con)
        fu = [d for d in q if d.get("nextFollowUp")]
        upcoming = [dict(r) for r in con.execute(
            "SELECT county, state, source_type, address, event_date, judgment_amount, assessed_value FROM leads WHERE status='new' AND event_date>=? AND event_date<=? ORDER BY event_date",
            (t, (now.date() + timedelta(days=21)).isoformat()))]
        for u in upcoming:
            u["days_left"] = (date.fromisoformat(u["event_date"]) - now.date()).days
        no_addr = sum(1 for u in upcoming if not (u.get("address") or "").strip())
        try:
            top = [dict(r) for r in con.execute(
                """SELECT p.street, p.city, p.county, s.score, s.tier, s.next_action, s.missing FROM lead_scores s JOIN hunter_leads h ON h.property_id=s.property_id
                   JOIN properties p ON p.id=s.property_id WHERE h.status='new' AND s.tier='A' ORDER BY s.score DESC LIMIT 5""")]
            tiers = {r[0]: r[1] for r in con.execute("SELECT s.tier, COUNT(*) FROM lead_scores s JOIN hunter_leads h ON h.property_id=s.property_id WHERE h.status='new' GROUP BY s.tier")}
        except Exception:  # noqa: BLE001
            top, tiers = [], {}
        expired = con.execute("SELECT COUNT(*) FROM leads WHERE status='expired'").fetchone()[0]
        tasks = [x for x in crm.items(con, "tasks") if not x.get("done") and x.get("due") and x["due"] <= t]
        deals = [d for d in crm.items(con, "deals") if not d.get("archived")]
        underwritten = [d for d in deals if d.get("arv")]
        return {
            "acquisitions": {"county_leads_waiting": con.execute("SELECT COUNT(*) FROM leads WHERE status='new' AND " + db.UPCOMING).fetchone()[0],
                             "county_leads_expired_total": expired,
                             "expired_note": "Leads whose sale date passed are set aside automatically each day (status 'expired').",
                             "upcoming_sales_21d": upcoming[:10], "upcoming_sales_21d_count": len(upcoming), "upcoming_missing_address": no_addr,
                             "upcoming_with_address": [u for u in upcoming if (u.get("address") or "").strip()][:5],
                             "upcoming_by_week": {"0-7 days": sum(1 for u in upcoming if u["days_left"] <= 7),
                                                  "8-14 days": sum(1 for u in upcoming if 7 < u["days_left"] <= 14),
                                                  "15-21 days": sum(1 for u in upcoming if u["days_left"] > 14)},
                             "scored_leads_by_tier": tiers, "top_a_tier": top},
            "underwriting": {"deals_with_arv": len(underwritten), "deals_total": len(deals),
                             "settings": {k: crm.settings(con).get(k) for k in ("maoPct", "rehab", "defaultFee")}},
            "seller_pipeline": {"by_stage": ov["countsByStage"], "leads_not_contacted": len(q) - len(fu),
                                "follow_ups_due": len(fu), "follow_ups_overdue": sum(1 for d in fu if d["nextFollowUp"] < t),
                                "follow_ups_top": [{"address": d.get("address"), "due": d.get("nextFollowUp"), "overdue": d["nextFollowUp"] < t, "stage": d.get("stage")} for d in fu[:3]],
                                "tasks_due": len(tasks), "tasks_overdue": sum(1 for x in tasks if x["due"] < t)},
            "dispositions": {"buyers": len(crm.items(con, "buyers")), "under_contract": ov["countsByStage"].get("Under Contract", 0),
                             "closing_scheduled": ov["countsByStage"].get("Closing Scheduled", 0)},
            "goal": {"goal": ov["goal"], "collected_this_month": ov["collectedThisMonth"], "projected_not_earned": ov.get("projectedNotEarned")},
            "deadlines": hq.deadlines(con, 14),
            "not_tracked": ["leads researched / qualified / rejected counts over time", "ARV, rehab and MAO per lead (only on deals once entered)",
                            "seller appointments", "offers made", "buyer interest and offers received", "assignments completed"],
        }
    _section(out, "molabucks", molabucks)

    # ---- approvals and problems (both businesses)
    def approvals():
        rows = [dict(r) for r in con.execute("SELECT agent, title, division, created FROM hq_tasks WHERE status='awaiting_approval' ORDER BY priority, created")]
        return {"count": len(rows), "top": rows[:5]}
    _section(out, "approvals", approvals)

    # ---- history: levels now vs saved snapshots
    def history():
        snaps = {r[0]: json.loads(r[1]) for r in con.execute("SELECT day, data FROM hq_daily_snapshot ORDER BY day DESC LIMIT 40")}
        def back(days):
            d = (now.date() - timedelta(days=days)).isoformat()
            return {"day": d, "levels": snaps[d]} if d in snaps else None
        return {"levels_now": levels(con), "yesterday": back(1), "week_ago": back(7), "month_ago": back(30),
                "snapshots_saved": len(snaps), "first_snapshot": min(snaps) if snaps else None}
    _section(out, "history", history)

    out["status"] = statuses(out, loop_ok)
    out["decisions_inputs"] = _signals(out, loop_ok)
    return out


def statuses(b, loop_ok):
    """Fixed rules. A critical component down is never reported as Healthy."""
    inf = b.get("infrastructure") or {}
    reasons = []
    if not loop_ok:
        overall = "Critical"
        reasons.append("work loop not running")
    else:
        overall = "Healthy"
    warn = []
    es = inf.get("email_sender") or {}
    if es.get("last_error"):
        warn.append(f"email sender error ({es.get('error_kind')})")
    if inf.get("incidents_open"):
        warn.append(f"{len(inf['incidents_open'])} open incident(s)")
    for r in inf.get("county_scrapers") or []:
        if r.get("error"):
            warn.append(f"scrape error: {r['target']}")
        elif r.get("age_hours") is not None and r["age_hours"] > 36:
            warn.append(f"scrape stale: {r['target']}")
    if inf.get("stuck_tasks"):
        warn.append("stuck tasks")
    if not (inf.get("database") or {}).get("ok", True):
        overall = "Critical"
        reasons.append("database check failed")
    ai = inf.get("ai_spend") or {}
    if ai.get("cap") and ai.get("spent_today", 0) >= 0.8 * ai["cap"]:
        warn.append("AI spend at 80%+ of cap")
    if b.get("sections_unavailable"):
        warn.append("some brief sections unavailable: " + ", ".join(b["sections_unavailable"]))
    if overall == "Healthy" and warn:
        overall = "Needs Attention"
    reasons += warn

    dg = b.get("demigod") or {}
    wf = b.get("workforce") or []
    o = dg.get("outreach") or {}
    queued_work = any((w.get("queued") or 0) + (w.get("waiting_on_dependencies") or 0) for w in wf if isinstance(w, dict))
    live = any(c.get("status") in ("sending", "approved") for c in dg.get("campaigns") or [])
    if not loop_ok and (queued_work or live):
        dgs = "Blocked"
    elif o.get("approvals_waiting") or o.get("ready_for_review"):
        dgs = "Waiting"
    elif o.get("follow_ups_due") or any(r.get("class") in ("interested", "meeting", "not read yet") for r in o.get("recent_replies") or []) or es.get("last_error"):
        dgs = "Needs Attention"
    elif not live and not o.get("sending_today") and not queued_work:
        dgs = "Idle"
    else:
        dgs = "Healthy"

    mb = b.get("molabucks") or {}
    sp = mb.get("seller_pipeline") or {}
    acq = mb.get("acquisitions") or {}
    soon = [u for u in acq.get("upcoming_sales_21d") or [] if u.get("days_left", 99) <= 14]
    act = ((b.get("activity") or {}).get("yesterday") or {}).get("molabucks") or {}
    if sp.get("follow_ups_overdue") or act.get("seller_texts_in") or soon or sp.get("tasks_overdue"):
        mbs = "Needs Attention"
    elif not sp.get("follow_ups_due") and not act.get("seller_texts_out") and not act.get("seller_texts_in"):
        mbs = "Idle"
    else:
        mbs = "Healthy"
    return {"overall": overall, "overall_reasons": reasons, "demigod": dgs, "molabucks": mbs}


def _signals(b, loop_ok):
    """Evidence the email writer can turn into decisions. Facts only; ranking is by business impact."""
    s = []
    inf = b.get("infrastructure") or {}
    if not loop_ok:
        wl = inf.get("work_loop") or {}
        s.append({"area": "reliability", "fact": f"Work loop not running; last tick {wl.get('last_tick')}, {wl.get('minutes_since_tick')} min ago", "owner": "Project Manager",
                  "needs_mola": True, "action": RECOVERY["loop_down"]})
    for i in inf.get("incidents_open") or []:
        s.append({"area": "reliability", "fact": f"{i['message']} (open {i['duration']}, seen {i['count']}x)", "owner": "QA Engineer", "needs_mola": i["area"] in ("gmail_auth", "loop_down"),
                  "action": i.get("recovery")})
    dg = b.get("demigod") or {}
    for bw in (dg.get("outreach") or {}).get("blocked_work") or []:
        if bw["n"] >= 3:
            s.append({"area": "revenue", "fact": f"{bw['n']} {bw['kind']} task(s) blocked (last {bw['last']}): {bw['sample_error'][:120]}", "owner": "SDR/Appointment Setter",
                      "needs_mola": True, "action": "Review the blocked drafts in the JARVIS app; redraft or drop them."})
    mb = b.get("molabucks") or {}
    acq = mb.get("acquisitions") or {}
    if acq.get("upcoming_sales_21d_count"):
        u = (acq.get("upcoming_with_address") or acq["upcoming_sales_21d"])[0]
        wk = acq.get("upcoming_by_week") or {}
        s.append({"area": "deal acquisition", "fact": f"{acq['upcoming_sales_21d_count']} lead(s) waiting for review go to sale within 21 days ({wk.get('0-7 days', 0)} within 7 days; "
                  f"{acq.get('upcoming_missing_address', 0)} missing an address); soonest with an address: {u.get('address') or 'none'} on {u['event_date']} ({u['days_left']} days)",
                  "owner": "SDR/Appointment Setter", "needs_mola": True, "action": "Review the auction-dated leads first; approve seller outreach for the ones worth working."})
    if (acq.get("scored_leads_by_tier") or {}).get("A"):
        s.append({"area": "deal acquisition", "fact": f"{acq['scored_leads_by_tier']['A']} A-tier Hunter leads not yet worked", "owner": "SDR/Appointment Setter", "needs_mola": True,
                  "action": "Work the top A-tier leads (skip trace, then outreach with approval)."})
    cs = dg.get("campaigns") or []
    if cs and not any(c.get("status") in ("sending", "approved", "draft", "review") for c in cs) and not (dg.get("outreach") or {}).get("ready_for_review"):
        s.append({"area": "revenue", "fact": f"No AI Sidekicks campaign active; last one ({cs[0]['name']}) sent {cs[0].get('sent')} with {cs[0].get('interested', 0)} interested",
                  "owner": "SDR/Appointment Setter", "needs_mola": True, "action": "Start the next prospect search and campaign in the JARVIS app."})
    return s
