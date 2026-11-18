"""Stage 2: standing duties, so AI Sidekicks' workforce keeps working without being asked.

Each duty belongs to one existing agent and runs on a schedule inside the HQ scheduler (durable: keyed by day/week in kv,
so a restart never runs a duty twice). A duty creates ordinary HQ tasks (source "duty"), so everything keeps its approvals,
retries, history and spend limits. No new employees; nothing here sends, publishes or buys anything.

  SDR        09:00 daily   discover 25 businesses, rotating through the approved industries and areas
             every 30 min  look up published emails for a finished discovery (company sites first; never guessed)
             10:00 daily   draft follow-up #2/#3 for anyone due (sending still needs your approval)
  Marketing  Mon 09:30     next week's content ideas, scripts and captions (drafts; publishing needs your approval)
  QA         07:30 daily   system check: engine heartbeat, email, scrapes, backups, data integrity, spend
  PM         17:30 daily   blockers: failed or stuck tasks, approvals waiting over a day, missed deadlines
  (system)   03:00 daily   database backup + restore test, 14 kept

Agents with nothing to do (no clients yet, no booked calls) are "idle, monitoring": the board says what they watch.
Per-agent daily AI budgets (inside the overall cap) stop any one agent from using the whole day's spend.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import db

INDUSTRIES = {  # label -> what the SDR searches for (approved Nov 12, 2026: start with these four, Greater Boston)
    "home services": "home service contractors: HVAC, plumbing and roofing companies",
    "property management": "property management companies (and real estate businesses that manage rentals)",
    "auto repair": "independent auto repair shops",
    "cleaning and restoration": "cleaning companies and water, fire or mold restoration companies",
}
DEFAULT_AREAS = ["Greater Boston, MA"]
DEFAULT_BUDGETS = {"sdr": 2.50, "marketing": 0.30, "ae": 0.20}  # USD per day, inside the overall AI cap ($3 approved for prospecting)
MONITORING = {
    "sdr": "discovery, email lookups and follow-ups due",
    "marketing": "next week's content plan",
    "ae": "booked calls and replies asking for a call (pre-call briefs), and proposal drafts from your call notes",
    "architect": "a demo page for each booked call; prospects who said yes (CRM plan or demo agent when you ask)",
    "builder": "approved builds (none yet)",
    "qa": "daily system check",
    "pm": "failed, stuck or overdue work",
    "account_mgr": "active clients (none yet)",
    "support": "client support requests (none yet)",
}
DUTIES = [  # (id, agent, title, schedule)
    ("sdr.discover", "sdr", "Discover new businesses (rotating industries)", "daily 09:00"),
    ("sdr.find_emails", "sdr", "Look up published emails for new businesses", "every 30 min"),
    ("sdr.linkedin", "sdr", "Pick 20 owners to connect with on LinkedIn and draft their notes (you send them)", "daily 08:00"),
    ("sdr.followups", "sdr", "Draft follow-ups that are due", "daily 10:00"),
    ("sdr.crm_sync", "sdr", "Add new businesses to the AI Sidekicks CRM, advance stages on evidence, rescore", "every 30 min"),
    ("marketing.weekly", "marketing", "Next week's content plan", "Mondays 09:30"),
    ("qa.system_check", "qa", "System check", "daily 07:30"),
    ("pm.daily_status", "pm", "Blockers and overdue work", "daily 17:30"),
]
BACKUPS_KEPT = 14


def conf(cfg):
    p = ((cfg or {}).get("hq") or {}).get("prospecting") or {}
    inds = [i for i in (p.get("industries") or list(INDUSTRIES)) if str(i).strip()]
    return {"enabled": p.get("enabled", True), "industries": inds, "areas": p.get("areas") or DEFAULT_AREAS,
            "per_day": max(5, min(50, int(p.get("per_day") or 25))), "followups_per_day": int(p.get("followups_per_day") or 25)}


def industry_phrase(label):
    return INDUSTRIES.get(str(label or "").strip().lower()) or str(label or "").strip()


# ------------------------------------------------------------------ budgets
def budgets(con):
    try:
        b = json.loads(db.kv_get(con, "ai_agent_budgets") or "null")
    except ValueError:
        b = None
    return {**DEFAULT_BUDGETS, **(b or {})}


def set_budgets(con, values):
    clean = {}
    for k, v in (values or {}).items():
        v = float(v)
        if not 0 <= v <= 50:
            raise ValueError("A daily budget must be between $0 and $50.")
        clean[str(k)] = round(v, 2)
    db.kv_set(con, "ai_agent_budgets", json.dumps({**budgets(con), **clean}))
    return budgets(con)


def spent_by(con, agent, day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    try:
        return float(con.execute("SELECT COALESCE(SUM(cost),0) FROM hq_ai_usage WHERE day=? AND agent=?", (day, agent)).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0.0


# ------------------------------------------------------------------ the duties
def _due(con, key, at, hm):
    return at.strftime("%H:%M") >= hm and db.kv_get(con, key) != at.strftime("%Y-%m-%d")


def discover(con, cfg, at, log=print):
    from . import hq
    c = conf(cfg)
    if not c["enabled"] or not c["industries"]:
        return None
    running = [s for s in hq.searches(con, 20)["searches"] if s.get("source") == "duty" and not s["done"]]
    if running:
        return None  # one discovery at a time; the next waits for this one
    if spent_by(con, "sdr") >= budgets(con).get("sdr", 0):
        return None
    combos = [(i, a) for a in c["areas"] for i in c["industries"]]
    k = int(db.kv_get(con, "duty_discover_i") or 0)
    ind, area = combos[k % len(combos)]
    s = hq.search_start(con, area, c["per_day"], "duty", industry=ind)
    db.kv_set(con, "duty_discover_i", str(k + 1))
    log(f"[{at:%H:%M}] SDR duty: finding {c['per_day']} {ind} businesses in {area}")
    return s["id"]


def find_emails(con, cfg, at, log=print):
    from . import hq
    for s in hq.searches(con, 20)["searches"]:
        if s.get("source") != "duty" or not s["done"] or s["lookups"] or s["found"] == s["with_email"] or s["found"] == 0:
            continue
        if spent_by(con, "sdr") >= budgets(con).get("sdr", 0):
            return None
        try:
            hq.search_enrich(con, s["id"], "duty")
            log(f"[{at:%H:%M}] SDR duty: looking up emails for {s['found'] - s['with_email']} businesses ({s['area']})")
        except ValueError:
            continue
        return s["id"]
    return None


def followups(con, cfg, at, log=print):
    from . import hq
    made = []
    for d in hq.followups_due(con)["due"][:conf(cfg)["followups_per_day"]]:
        rec = hq.di_record_brief(con, prospect_id=d["prospect_id"], limit=8)  # true details to build the new reason on
        t = hq.create_task(con, "sdr.followup_draft", {"prospect_id": d["prospect_id"], "recordId": d["prospect_id"], "company": d["company"],
                                                       "to": d["to"], "step": d["step"], "thread_id": d["thread_id"], "previous": d["previous"],
                                                       "record": {k: rec[k] for k in ("industry", "city", "services", "facts") if rec.get(k)}},
                           title=f"Follow-up #{d['step']} · {d['company']}"[:160], division="demigod", source="duty",
                           dedupe_key=f"duty.fu:{d['prospect_id']}:{d['step']}")
        made.append(t["id"])
    if made:
        log(f"[{at:%H:%M}] SDR duty: drafting {len(made)} follow-up(s) for your review")
    return made


def marketing_weekly(con, cfg, at, log=print):
    from . import hq
    c = conf(cfg)
    wk = at.strftime("%G-W%V")
    t = hq.create_task(con, "marketing.content_draft", {
        "topic": "Next week's organic content for AI Sidekicks (AI assistants that answer every call, text and follow-up): 3 LinkedIn posts, "
                 "3 short-video scripts (founder on camera or a screen recording of a real JARVIS feature), 1 carousel outline.",
        "audience": "Owners and managers of small and midsize businesses: " + ", ".join(c["industries"]) + ".",
        "pillars": ["AI for business operations", "custom CRM systems", "AI assistants", "workflow automation", "demos and build-in-public"],
        "format_rules": "Each item: target audience, business problem, hook (first line), central message, CTA, how to film or design it, where to post. "
                        "CTA = comment a word tied to the content (e.g. Comment SYSTEM). Use only real features JARVIS has today. "
                        "No invented clients, results, numbers or testimonials. No Higgsfield or AI-generated video of people.",
        "week": wk}, title=f"Content plan · week {wk}", division="demigod", source="duty", dedupe_key=f"duty.mkt:{wk}")
    log(f"[{at:%H:%M}] Marketing duty: next week's content plan drafting")
    return t["id"]


def _age_min(ts, now):
    try:
        return (now - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def ex_system_check(con, cfg, t):
    """QA: is everything actually running? Facts from the database and kv only."""
    from . import hq, agents_ai
    now = datetime.now()
    problems, ok = [], []
    hb = _age_min(db.kv_get(con, "hq_last_tick"), now)
    (ok if hb is not None and hb < 5 else problems).append(f"Engine heartbeat {'%.0f min ago' % hb if hb is not None else 'never'}")
    try:
        es = hq.engine_sender(con)
        if es.get("configured") and es.get("last_error"):
            problems.append("Email: " + str(es["last_error"])[:160])
        elif es.get("configured"):
            rc = (es.get("reply_check") or {})
            age = _age_min(rc.get("at"), now)
            (ok if age is not None and age < 60 else problems).append(f"Reply checker last ran {'%.0f min ago' % age if age is not None else 'never'}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"Email status unreadable: {e}")
    day = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    ls = db.kv_get(con, "last_scrape_day")
    (ok if ls in (day, yday) else problems).append(f"County scrape last ran {ls or 'never'}")
    try:
        bk = json.loads(db.kv_get(con, "backup_last") or "{}")
    except ValueError:
        bk = {}
    bage = _age_min(bk.get("at"), now)
    (ok if bk.get("ok") and bage is not None and bage < 26 * 60 else problems).append(
        f"Backup {'verified' if bk.get('ok') else 'NOT verified'} {('%.0f h ago' % (bage / 60)) if bage is not None else 'never'}")
    try:
        integ = hq.integrity(con)
        issues = integ.get("problems") or integ.get("issues") or []
        if issues:
            problems.append(f"Data integrity: {len(issues)} issue(s)")
        else:
            ok.append("Data integrity checks pass")
    except Exception as e:  # noqa: BLE001
        problems.append(f"Integrity check failed to run: {e}")
    failed = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status IN ('failed','blocked') AND updated>=?", ((now - timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchone()[0]
    if failed:
        problems.append(f"{failed} task(s) failed or blocked in the last 24 h")
    sp, cap = agents_ai.spent(con), agents_ai.cap(con)
    ok.append(f"AI spend yesterday-to-now ${sp:.2f} of ${cap:.2f} today")
    body = {"problems": problems, "ok": ok, "at": hq.now()}
    db.kv_set(con, "system_check_last", json.dumps(body))
    s = ("All systems normal." if not problems else f"{len(problems)} problem(s): " + "; ".join(problems))[:600]
    return {"verified": True, "summary": s, "result": body}


def ex_daily_status(con, cfg, t):
    """PM: what's stuck. From the task table only."""
    from . import hq
    now = datetime.now()
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    failed = [dict(r) for r in con.execute("SELECT id, agent, title, status, error FROM hq_tasks WHERE status IN ('failed','blocked') AND updated>=? ORDER BY updated DESC LIMIT 20", (day_ago,))]
    waiting = [dict(r) for r in con.execute("SELECT id, agent, title, created FROM hq_tasks WHERE status='awaiting_approval' AND created<? ORDER BY created LIMIT 20", (day_ago,))]
    stuck = [dict(r) for r in con.execute("SELECT id, agent, title, created FROM hq_tasks WHERE status='queued' AND created<? ORDER BY created LIMIT 20",
                                          ((now - timedelta(hours=6)).isoformat(timespec="seconds"),))]
    overdue = [dict(r) for r in con.execute("SELECT id, agent, title, deadline FROM hq_tasks WHERE deadline IS NOT NULL AND deadline<? AND status NOT IN ('completed','cancelled','rejected') LIMIT 20", (hq.now(),))]
    done = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='completed' AND updated>=?", (day_ago,)).fetchone()[0]
    body = {"completed_24h": done, "failed_or_blocked": failed, "approvals_over_a_day": waiting, "queued_over_6h": stuck, "past_deadline": overdue}
    parts = [f"{done} done in 24 h"] + [f"{len(v)} {k.replace('_', ' ')}" for k, v in body.items() if isinstance(v, list) and v]
    return {"verified": True, "summary": ", ".join(parts) + ".", "result": body}


KINDS = {"qa.system_check": ("qa", 1, "System check", 120), "pm.daily_status": ("pm", 1, "Blockers and overdue work", 60)}
EXECUTORS = {"qa.system_check": ex_system_check, "pm.daily_status": ex_daily_status}


# ------------------------------------------------------------------ backups
def backup_daily(con, at=None, log=print):
    """Full copy of the database, then a restore test on the copy: it opens, passes integrity_check, and holds the same rows."""
    from . import hq
    at = at or datetime.now()
    path = hq.backup(con, "daily")
    res = {"at": at.isoformat(timespec="seconds"), "path": str(path), "ok": False}
    try:
        b = sqlite3.connect(str(path))
        chk = b.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for tbl in ("hq_tasks", "leads", "crm_items"):
            try:
                live = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                copy = b.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                counts[tbl] = [live, copy]
            except sqlite3.OperationalError:
                pass
        b.close()
        res.update(integrity=chk, counts=counts, size=Path(path).stat().st_size,
                   ok=chk == "ok" and all(c >= l - 5 for l, c in counts.values()))  # a few rows may land between the copy and the count
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{e.__class__.__name__}: {e}"
    kept = sorted(Path(path).parent.glob("*daily*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in kept[BACKUPS_KEPT:]:
        try:
            old.unlink()
        except OSError:
            pass
    db.kv_set(con, "backup_last", json.dumps(res))
    if not res["ok"]:
        log(f"[{at:%H:%M}] Backup check FAILED: {res.get('error') or res.get('integrity')}")
    return res


# ------------------------------------------------------------------ the scheduler hook (called from hq.schedule_tick)
def linkedin_list(con, cfg, at, log=print):
    from . import linkedin
    if not conf(cfg)["enabled"]:
        return None
    r = linkedin.build(con)
    if r.get("added"):
        log(f"[{at:%H:%M}] SDR duty: {r['added']} LinkedIn owners ready for you to connect with ({r.get('ai_drafted', 0)} notes drafted by AI)")
    return r.get("added")


def tick(con, cfg, at=None, log=print):
    from . import hq
    at = (at or datetime.now()).replace(tzinfo=None)
    day = at.strftime("%Y-%m-%d")
    made = []

    def once(key, hm, fn):
        if _due(con, key, at, hm):
            db.kv_set(con, key, day)  # marked first: a duty that errors is retried tomorrow, not every 30 seconds
            try:
                r = fn(con, cfg, at, log)
                if r:
                    made.append(key)
            except Exception as e:  # noqa: BLE001  one duty failing never stops the others
                log(f"[{at:%H:%M}] duty {key} error: {e}")

    once("duty_backup_day", "03:00", lambda c, cf, a, lg: backup_daily(c, a, lg))
    once("duty_qa_day", "07:30", lambda c, cf, a, lg: hq.create_task(c, "qa.system_check", {"day": day}, division="demigod", source="duty", dedupe_key=f"duty.qa:{day}")["id"])
    once("duty_discover_day", "09:00", discover)
    once("duty_followups_day", "10:00", followups)
    once("duty_li_day", "08:00", linkedin_list)
    once("duty_pm_day", "17:30", lambda c, cf, a, lg: hq.create_task(c, "pm.daily_status", {"day": day}, division="demigod", source="duty", dedupe_key=f"duty.pm:{day}")["id"])
    if at.weekday() == 0 and at.strftime("%H:%M") >= "09:30" and db.kv_get(con, "duty_mkt_week") != at.strftime("%G-W%V"):
        db.kv_set(con, "duty_mkt_week", at.strftime("%G-W%V"))
        try:
            marketing_weekly(con, cfg, at, log)
            made.append("duty_mkt_week")
        except Exception as e:  # noqa: BLE001
            log(f"[{at:%H:%M}] duty marketing error: {e}")
    lastc = db.kv_get(con, "duty_crm_at") or ""
    if not lastc or (_age_min(lastc, at) or 99) >= 30:
        db.kv_set(con, "duty_crm_at", at.isoformat(timespec="seconds"))
        try:
            from . import dcrm
            r = dcrm.sync(dcrm.connect(), do_enrich=False)  # website research runs in its own thread (run.py), never in this loop
            if r.get("new") or r.get("stage_moves"):
                log(f"[{at:%H:%M}] SDR duty: CRM +{r['new']} businesses ({r['merged']} merged), {r['stage_moves']} stage move(s) on evidence")
                made.append("duty_crm")
        except Exception as e:  # noqa: BLE001
            log(f"[{at:%H:%M}] duty crm error: {e}")
    last = db.kv_get(con, "duty_emails_at") or ""
    if not last or (_age_min(last, at) or 99) >= 30:
        db.kv_set(con, "duty_emails_at", at.isoformat(timespec="seconds"))
        try:
            if find_emails(con, cfg, at, log):
                made.append("duty_emails")
        except Exception as e:  # noqa: BLE001
            log(f"[{at:%H:%M}] duty emails error: {e}")
    return made


def board(con):
    """What each agent's standing job is, for the HQ board and voice."""
    out = {}
    for d, agent, title, when in DUTIES:
        out.setdefault(agent, []).append({"duty": d, "title": title, "schedule": when})
    return out
