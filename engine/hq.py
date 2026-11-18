"""MolaBucks Capital HQ: the agents' operating backbone.

JARVIS (CEO) turns a goal into a plan and assigns tasks to six specialist agents. Every task is a durable row with
an id, owner, objective, status, timestamps, attempts, events and a verifiable result. Workers lease tasks so two
processes (the engine loop and the MCP server) never run the same task twice. Restarting recovers expired leases.

Policy levels
  1  internal work (read records, score, match, summarize, create internal tasks)  -> runs on its own
  2  prepares outside-facing drafts (messages, offer recommendations)               -> runs, output is a draft
  3  outside commitments (launch a texting campaign, spend money)                  -> waits for Mola's approval
An approval covers exactly the parameters reviewed (hash-bound). Changing them voids it. No agent can approve,
change policies or its own permissions; approvals only come from the owner's action in the JARVIS app.
"""
import hashlib
import json
import math
import re
import os
import socket
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import crm, db, agents_ai, org

SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_tasks(
  id TEXT PRIMARY KEY, agent TEXT, kind TEXT, title TEXT, objective TEXT, params TEXT DEFAULT '{}', params_hash TEXT,
  priority INTEGER DEFAULT 3, status TEXT, deadline TEXT, depends_on TEXT DEFAULT '[]', goal_id TEXT, parent_id TEXT,
  source TEXT, dedupe_key TEXT, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3, next_run_at TEXT,
  lease_owner TEXT, lease_until TEXT, created TEXT, updated TEXT, started TEXT, finished TEXT,
  result TEXT, summary TEXT, error TEXT, recovery TEXT, verified INTEGER DEFAULT 0, verify_note TEXT,
  level INTEGER DEFAULT 1, approval_state TEXT DEFAULT 'none', approved_hash TEXT, approved_at TEXT, approved_by TEXT,
  test INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS hq_dedupe ON hq_tasks(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS hq_status ON hq_tasks(status, next_run_at);
CREATE TABLE IF NOT EXISTS hq_events(id INTEGER PRIMARY KEY, task_id TEXT, agent TEXT, at TEXT, type TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS hq_ev_task ON hq_events(task_id);
CREATE TABLE IF NOT EXISTS hq_goals(id TEXT PRIMARY KEY, text TEXT, target REAL, period TEXT, status TEXT, created TEXT, plan TEXT);
CREATE TABLE IF NOT EXISTS hq_reports(id TEXT PRIMARY KEY, kind TEXT, at TEXT, title TEXT, body TEXT, task_id TEXT);
CREATE TABLE IF NOT EXISTS hq_chats(id INTEGER PRIMARY KEY, agent TEXT, at TEXT, role TEXT, text TEXT, task_id TEXT);
"""
ACTIVE = ("queued", "running", "waiting", "awaiting_approval")
TERMINAL = ("completed", "failed", "cancelled", "rejected")
WORKER = f"{socket.gethostname()}:{os.getpid()}"

AGENTS = org.ROSTER  # one registry for both businesses (engine/org.py)

# kind -> (agent, policy level, title, timeout seconds)
KINDS = {
    "jarvis.plan": ("jarvis", 1, "Build the operating plan for the goal", 120),
    "jarvis.briefing": ("jarvis", 1, "Morning briefing: assign today's work", 120),
    "jarvis.briefing_report": ("jarvis", 1, "Write the morning briefing", 120),
    "jarvis.eod": ("jarvis", 1, "End-of-day review", 120),
    "hunter.sync_sources": ("hunter", 1, "Sync county record sources", 3600),
    "hunter.top_leads": ("hunter", 1, "Prioritize the strongest leads", 120),
    "hunter.review_backlog": ("hunter", 1, "Check leads waiting for review", 60),
    "closer.pipeline_review": ("closer", 1, "Review contracted deals for blockers", 60),
    "dispo.match_buyers": ("dispo", 1, "Match deals to buyers", 120),
    "followup.due_list": ("followup", 1, "List who's due for follow-up", 60),
    "followup.campaign_health": ("followup", 1, "Check campaign and reply health", 60),
    "followup.launch_campaign": ("followup", 3, "Launch a texting campaign", 120),
    "frontdesk.triage": ("frontdesk", 1, "Triage incoming messages", 60),
    "finance.check": ("finance", 1, "Finance check: fees, deadlines, balances", 120),
    # AI Sidekicks outreach email: always needs your approval for these exact words; the JARVIS app sends it from your Gmail
    "sdr.send_email": ("sdr", 3, "Send an outreach email", 60),
    # Phase 10: other outside actions. Each needs your approval for its exact details; the JARVIS app carries it out
    # through your connector (Google Calendar, DocuSign, ElevenLabs) and reports the result back here.
    "ae.book_meeting": ("ae", 3, "Book a meeting", 60),
    "ae.send_agreement": ("ae", 3, "Send the agreement for signature", 60),
    "architect.build_demo": ("architect", 3, "Build a demo agent", 60),
}


KINDS.update(agents_ai.KINDS)  # Phase 4: Executive Office and AI Sidekicks roles get real (AI-drafting) executors
from . import duties  # noqa: E402  Stage 2: standing duties (QA system check, PM blockers) + the scheduler hook
KINDS.update(duties.KINDS)


def now():
    return datetime.now().isoformat(timespec="seconds")


def _j(v):
    return json.dumps(v, default=str)


def _h(params):
    return hashlib.sha256(json.dumps(params or {}, sort_keys=True, default=str).encode()).hexdigest()[:24]


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    _migrate(con)
    agents_ai.ensure(con)
    con.executescript(REPLY_SCHEMA)
    con.executescript(CAMPAIGN_SCHEMA)
    con.executescript(SEARCH_SCHEMA)
    try:
        con.execute("ALTER TABLE hq_prospect_searches ADD COLUMN enrich_tasks TEXT")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE hq_prospect_searches ADD COLUMN industry TEXT")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE hq_campaign_items ADD COLUMN info TEXT")
    except Exception:
        pass
    return con


def backup(con, label="backup"):
    """Copy the whole database to data/backups/ before any schema change. Returns the file path."""
    from pathlib import Path
    import sqlite3
    dest_dir = Path(db.DB_PATH).parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"jarvis-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"
    out = sqlite3.connect(dest)
    con.backup(out)
    out.close()
    return str(dest)


def _migrate(con):
    """Additive only. JARVIS 2.0: every task names the business it's for (existing tasks are MolaBucks)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(hq_tasks)")}
    if "division" not in cols:
        has_rows = con.execute("SELECT COUNT(*) FROM hq_tasks").fetchone()[0]
        if has_rows:
            path = backup(con, "pre-division")
            db.kv_set(con, "hq_last_backup", path)
        con.execute("ALTER TABLE hq_tasks ADD COLUMN division TEXT DEFAULT 'molabucks'")
        con.execute("CREATE INDEX IF NOT EXISTS hq_division ON hq_tasks(division, status)")
        con.commit()


def event(con, task_id, agent, type_, message, data=None):
    con.execute("INSERT INTO hq_events(task_id,agent,at,type,message,data) VALUES(?,?,?,?,?,?)",
                (task_id, agent, now(), type_, str(message)[:500], _j(data) if data is not None else None))


# ---------------------------------------------------------------- task lifecycle
def create_task(con, kind, params=None, *, title=None, objective="", priority=3, deadline=None, depends_on=None,
                goal_id=None, parent_id=None, source="owner", dedupe_key=None, test=False, run_at=None, division=None):
    if kind not in KINDS:
        raise ValueError(f"Unknown task type: {kind}")
    agent, level, default_title, _ = KINDS[kind]
    business = org.check_assignment(agent, division)  # refuses cross-business work before anything is written
    params = params or {}
    if kind in ACTION_PARAMS:
        params = ACTION_PARAMS[kind](con, params)
    if kind == "architect.demo_spec":
        demo_gate(con, params.get("recordId") or params.get("prospect_id"))
    if kind == "architect.crm_plan" and params.get("recordKind", "prospect") == "prospect" and not said_yes(con, params.get("recordId") or params.get("prospect_id")):
        raise ValueError("CRM plans are only made for companies that said yes: a reply read as interested or a meeting, or a booked call.")
    if dedupe_key:
        cur = con.execute("SELECT * FROM hq_tasks WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if cur:
            return dict(cur)
    deps = [d for d in (depends_on or []) if d]
    for d in deps:
        if not con.execute("SELECT 1 FROM hq_tasks WHERE id=?", (d,)).fetchone():
            raise ValueError(f"Dependency {d} doesn't exist.")
    tid = "t_" + uuid.uuid4().hex[:12]
    status = "awaiting_approval" if level >= 3 else "queued"
    con.execute("""INSERT INTO hq_tasks(id,agent,kind,title,objective,params,params_hash,priority,status,deadline,depends_on,goal_id,
                   parent_id,source,dedupe_key,next_run_at,created,updated,level,approval_state,test,division)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, agent, kind, (title or default_title)[:160], str(objective)[:600], _j(params), _h(params),
                 max(1, min(int(priority or 3), 5)), status, deadline, _j(deps), goal_id, parent_id, source, dedupe_key,
                 run_at or now(), now(), now(), level, "pending" if level >= 3 else "none", 1 if test else 0, business))
    event(con, tid, agent, "created", f"Created by {source}" + (" (needs your approval)" if level >= 3 else ""), {"params": params})
    con.commit()
    return get_task(con, tid, events=False)


def get_task(con, tid, events=True):
    r = con.execute("SELECT * FROM hq_tasks WHERE id=?", (tid,)).fetchone()
    if not r:
        raise ValueError("Task not found.")
    t = dict(r)
    for k in ("params", "result", "depends_on"):
        try:
            t[k] = json.loads(t[k]) if t[k] else ({} if k != "depends_on" else [])
        except ValueError:
            pass
    if events:
        t["events"] = [dict(e) for e in con.execute("SELECT at,type,message FROM hq_events WHERE task_id=? ORDER BY id", (tid,))]
    return t


def list_tasks(con, agent=None, statuses=None, limit=100, goal_id=None, division=None):
    q, a = "SELECT * FROM hq_tasks WHERE 1=1", []
    if division:
        q += " AND division=?"
        a.append(division)
    if agent:
        q += " AND agent=?"
        a.append(agent)
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        a += list(statuses)
    if goal_id:
        q += " AND goal_id=?"
        a.append(goal_id)
    q += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'awaiting_approval' THEN 1 WHEN 'waiting' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END, priority, updated DESC LIMIT ?"
    a.append(int(limit))
    out = []
    for r in con.execute(q, a):
        t = dict(r)
        t["params"] = json.loads(t["params"] or "{}")
        t.pop("result", None)
        out.append(t)
    return out


def _set(con, tid, **kw):
    kw["updated"] = now()
    con.execute(f"UPDATE hq_tasks SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), tid))


def cancel(con, tid, by="owner"):
    t = get_task(con, tid, events=False)
    if t["status"] in TERMINAL:
        raise ValueError(f"This task is already {t['status']}.")
    if t["status"] == "running":
        raise ValueError("This task is running right now. It can't be stopped mid-run; it will finish in a moment.")
    _set(con, tid, status="cancelled", finished=now(), lease_owner=None, lease_until=None)
    event(con, tid, t["agent"], "cancelled", f"Cancelled by {by}")
    con.commit()
    return get_task(con, tid, events=False)


def retry(con, tid):
    t = get_task(con, tid, events=False)
    if t["status"] not in ("failed", "blocked", "cancelled"):
        raise ValueError("Only failed, blocked or cancelled tasks can be retried.")
    if t["level"] >= 3 and t["approval_state"] != "approved":
        raise ValueError("This one needs your approval first.")
    _set(con, tid, status="queued", attempts=0, next_run_at=now(), error=None, recovery=None, finished=None)
    event(con, tid, t["agent"], "retry", "Re-queued by owner")
    con.commit()
    return get_task(con, tid, events=False)


def reprioritize(con, tid, priority):
    t = get_task(con, tid, events=False)
    if t["status"] in TERMINAL:
        raise ValueError("This task is finished.")
    _set(con, tid, priority=max(1, min(int(priority), 5)))
    event(con, tid, t["agent"], "priority", f"Priority set to {priority}")
    con.commit()
    return get_task(con, tid, events=False)


def approve(con, tid, params_hash, by="owner"):
    """Owner approval from the app. Binds to the exact parameters that were reviewed."""
    t = get_task(con, tid, events=False)
    if t["status"] != "awaiting_approval":
        raise ValueError("This task isn't waiting for approval.")
    if params_hash != t["params_hash"]:
        raise ValueError("This task changed since you reviewed it. Review it again.")
    _set(con, tid, status="queued", approval_state="approved", approved_hash=t["params_hash"], approved_at=now(),
         approved_by=by, next_run_at=now())
    event(con, tid, t["agent"], "approved", f"Approved by {by}")
    con.commit()
    return get_task(con, tid, events=False)


def reject(con, tid, reason="", by="owner"):
    t = get_task(con, tid, events=False)
    if t["status"] != "awaiting_approval":
        raise ValueError("This task isn't waiting for approval.")
    _set(con, tid, status="rejected", approval_state="rejected", finished=now())
    event(con, tid, t["agent"], "rejected", f"Rejected by {by}" + (f": {reason[:200]}" if reason else ""))
    con.commit()
    return get_task(con, tid, events=False)


# ---------------------------------------------------------------- worker
def _deps_state(con, t):
    deps = json.loads(t["depends_on"] or "[]")
    if not deps:
        return "ok"
    st = [r["status"] for r in con.execute(f"SELECT status FROM hq_tasks WHERE id IN ({','.join('?' * len(deps))})", deps)]
    if any(s in ("failed", "cancelled", "rejected") for s in st):
        return "broken"
    return "ok" if all(s == "completed" for s in st) and len(st) == len(deps) else "wait"


def recover(con, at=None):
    """Stale-lease recovery: a task whose worker died is retried (or failed after max attempts)."""
    at = at or now()
    n = 0
    for t in con.execute("SELECT * FROM hq_tasks WHERE status='running' AND lease_until<?", (at,)).fetchall():
        if t["attempts"] < t["max_attempts"]:
            _set(con, t["id"], status="queued", lease_owner=None, lease_until=None, next_run_at=at)
            event(con, t["id"], t["agent"], "recovered", "Worker stopped mid-task; re-queued")
        else:
            _set(con, t["id"], status="failed", lease_owner=None, lease_until=None, finished=at,
                 error="Worker stopped mid-task too many times.", recovery="Check the engine window for errors, then retry.")
            event(con, t["id"], t["agent"], "failed", "Gave up after repeated interruptions")
        n += 1
    con.commit()
    return n


def _lease(con, t, at):
    timeout = KINDS.get(t["kind"], (None, None, None, 300))[3]
    until = (datetime.fromisoformat(at) + timedelta(seconds=timeout)).isoformat(timespec="seconds")
    cur = con.execute("""UPDATE hq_tasks SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1,
                         started=COALESCE(started, ?), updated=? WHERE id=? AND status IN ('queued','waiting')""",
                      (WORKER, until, at, at, t["id"]))
    con.commit()
    return cur.rowcount == 1


def paused(con):
    """Global pause: the HQ switch, or the CRM emergency stop. Enforced here, in the worker, not in the app."""
    if db.kv_get(con, "hq_paused"):
        return "HQ automation is paused"
    try:
        if crm.settings(con).get("emergencyStop"):
            return "Emergency stop is on in JARVIS"
    except Exception:  # noqa: BLE001
        pass
    return ""


def set_paused(con, on, by="owner", reason=""):
    db.kv_set(con, "hq_paused", now() if on else "")
    event(con, None, "jarvis", "paused" if on else "resumed", (f"Automation paused by {by}" if on else f"Automation resumed by {by}") + (f": {reason[:200]}" if reason else ""))
    con.commit()
    return {"paused": bool(on), "reason": paused(con)}


def run_due(con, cfg, max_tasks=6, at=None, log=print):
    """Runs tasks that are due. One running task per agent at a time. Returns ids processed.
    While paused, nothing new starts; a task already running finishes (the worker is single-threaded, so at most one)."""
    at = at or now()
    recover(con, at)
    if paused(con):
        return []
    done = []
    busy = {r["agent"] for r in con.execute("SELECT agent FROM hq_tasks WHERE status='running'")}
    due = con.execute("""SELECT * FROM hq_tasks WHERE status IN ('queued','waiting') AND (next_run_at IS NULL OR next_run_at<=?)
                         ORDER BY priority, created LIMIT 50""", (at,)).fetchall()
    for t in due:
        if len(done) >= max_tasks:
            break
        if t["agent"] in busy:
            continue
        ds = _deps_state(con, t)
        if ds == "broken":
            _set(con, t["id"], status="blocked", error="A task this depends on failed or was cancelled.",
                 recovery="Retry or cancel the failed dependency, then retry this one.")
            event(con, t["id"], t["agent"], "blocked", "Dependency failed")
            con.commit()
            continue
        if ds == "wait":
            if t["status"] != "waiting":
                _set(con, t["id"], status="waiting", verify_note="Waiting for another task to finish")
                event(con, t["id"], t["agent"], "waiting", "Waiting on a dependency")
                con.commit()
            continue
        if t["level"] >= 3 and (t["approval_state"] != "approved" or t["approved_hash"] != t["params_hash"]):
            _set(con, t["id"], status="awaiting_approval", approval_state="pending")
            event(con, t["id"], t["agent"], "approval", "Needs your approval (parameters changed or never approved)")
            con.commit()
            continue
        if not _lease(con, t, at):
            continue
        busy.add(t["agent"])
        event(con, t["id"], t["agent"], "started", f"Attempt {t['attempts'] + 1}")
        con.commit()
        _execute(con, cfg, get_task(con, t["id"], events=False), at, log)
        busy.discard(t["agent"])
        done.append(t["id"])
    return done


def _execute(con, cfg, t, at, log):
    fn = EXECUTORS.get(t["kind"])
    try:
        out = fn(con, cfg, t)
    except Exception as e:  # noqa: BLE001
        msg = f"{e.__class__.__name__}: {e}" if not isinstance(e, ValueError) else str(e)
        cur = con.execute("SELECT attempts, max_attempts FROM hq_tasks WHERE id=?", (t["id"],)).fetchone()
        permanent = isinstance(e, PermissionError) or getattr(e, "permanent", False)
        if cur["attempts"] < cur["max_attempts"] and not permanent:
            wait = 60 * (2 ** (cur["attempts"] - 1))
            nxt = (datetime.fromisoformat(at) + timedelta(seconds=wait)).isoformat(timespec="seconds")
            _set(con, t["id"], status="queued", lease_owner=None, lease_until=None, next_run_at=nxt, error=msg[:500])
            event(con, t["id"], t["agent"], "retry", f"Failed ({msg[:200]}); retrying in {wait // 60} min")
        else:
            _set(con, t["id"], status="failed", lease_owner=None, lease_until=None, finished=now(), error=msg[:500],
                 recovery=getattr(e, "recovery", "") or "Check the error, fix the cause, then retry from HQ.")
            event(con, t["id"], t["agent"], "failed", msg[:300])
        con.commit()
        log(f"[HQ] {t['kind']} error: {msg[:160]}")
        return
    state = out.get("state", "done")
    if state == "wait":
        nxt = (datetime.fromisoformat(at) + timedelta(seconds=int(out.get("next_in", 60)))).isoformat(timespec="seconds")
        _set(con, t["id"], status="waiting", lease_owner=None, lease_until=None, next_run_at=nxt, verify_note=out.get("note", ""),
             attempts=max(0, t["attempts"] - 1) if t["attempts"] else 0)
        event(con, t["id"], t["agent"], "waiting", out.get("note", "Waiting"))
    elif out.get("verified"):
        _set(con, t["id"], status="completed", lease_owner=None, lease_until=None, finished=now(), result=_j(out.get("result")),
             summary=str(out.get("summary", ""))[:600], verified=1, verify_note=out.get("note", ""), error=None)
        event(con, t["id"], t["agent"], "completed", out.get("summary", "Done"))
    else:
        _set(con, t["id"], status="blocked", lease_owner=None, lease_until=None, result=_j(out.get("result")),
             summary=str(out.get("summary", ""))[:600], verified=0, error=out.get("note") or "Result couldn't be verified.",
             recovery=out.get("recovery", "Check the inputs and retry."))
        event(con, t["id"], t["agent"], "blocked", out.get("note") or "Couldn't verify the result")
    con.commit()


# ---------------------------------------------------------------- executors (real work against real records)
def _ok(summary, result, note=""):
    return {"verified": True, "summary": summary, "result": result, "note": note}


def ex_hunter_sync(con, cfg, t):
    from . import hunter
    hcon = hunter.connect()
    hunter.sync_registry(hcon, cfg)
    p = t["params"]
    jobs = p.get("_jobs")
    if not jobs:
        srcs = [r["id"] for r in hcon.execute("SELECT id FROM hunter_sources WHERE enabled=1 AND paused=0 AND access_method!='manual'")
                if not p.get("county") or p["county"].lower() in r["id"]]
        if not srcs:
            return {"verified": False, "summary": "No automated sources to sync.", "result": {},
                    "note": "No enabled automated sources" + (f" for {p['county']}" if p.get("county") else "") + ".",
                    "recovery": "Add a county with run.py add-county, or upload records under The Hunter > Sources."}
        jobs = [hunter.request_job(hcon, s) for s in srcs]
        p["_jobs"] = jobs
        con.execute("UPDATE hq_tasks SET params=? WHERE id=?", (_j(p), t["id"]))  # internal bookkeeping, hash unchanged
        con.commit()
        return {"state": "wait", "note": f"Queued {len(jobs)} source sync job(s)", "next_in": 30}
    rows = [dict(r) for r in hcon.execute(f"SELECT id,source_id,status,fetched,accepted,new,duplicates,rejected,errors FROM hunter_jobs WHERE id IN ({','.join('?' * len(jobs))})", jobs)]
    if any(r["status"] in ("queued", "running") for r in rows):
        return {"state": "wait", "note": f"{sum(r['status'] in ('queued', 'running') for r in rows)} sync job(s) still running", "next_in": 60}
    ok = [r for r in rows if r["status"] == "done"]
    new = sum(r.get("new") or 0 for r in ok)
    s = f"Synced {len(ok)}/{len(rows)} source(s): {sum(r.get('fetched') or 0 for r in ok)} records fetched, {new} new."
    if not ok:
        return {"verified": False, "summary": s, "result": {"jobs": rows}, "note": "Every source sync failed.",
                "recovery": "Open The Hunter > Sources to see each source's last error."}
    return _ok(s, {"jobs": rows, "new": new})


def ex_hunter_top(con, cfg, t):
    from . import hunter
    p = t["params"]
    f = {"tiers": p.get("tiers") or ["A", "B"], "limit": min(int(p.get("limit") or 25), 100), "sort": "score"}
    if p.get("county"):
        f["county"] = p["county"]
    r = hunter.leads(hunter.connect(), f)
    rows = [{"property_id": x.get("property_id") or x.get("id"), "address": ", ".join(v for v in [x.get("street"), x.get("city")] if v),
             "county": x.get("county"), "score": x.get("score"), "tier": x.get("tier"), "records": x.get("categories"),
             "next_date": x.get("next_date"), "skip": x.get("skip_status"), "next_action": x.get("next_action")} for x in r["rows"]]
    s = (f"{len(rows)} lead(s) ready ({', '.join(f['tiers'])} tiers{', ' + p['county'] if p.get('county') else ''}) out of {r['total']} matching."
         if rows else f"No {'/'.join(f['tiers'])} leads match right now.")
    needs_skip = sum(1 for x in rows if x["skip"] in ("none", None))
    if needs_skip:
        s += f" {needs_skip} still need skip tracing."
    return _ok(s, {"leads": rows, "total": r["total"]}, "Counted directly from The Hunter's scored records.")


def ex_hunter_review(con, cfg, t):
    from . import hunter
    sm = hunter.summary(hunter.connect())
    s = f"{sm['review']} lead(s) waiting for review; {sm['skip_pending']} skip-trace batch(es) waiting for AvaData results; {sm['skip_never']} never skip-traced."
    return _ok(s, {k: sm[k] for k in ("review", "review_queue", "skip_pending", "skip_never", "contacts", "jobs_failed_7d")})


def _contracts(con):
    return [c for c in crm.items(con, "contracts") if c.get("status") not in ("Cancelled", "Draft")]


def _biz_days(start, n):
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def deadlines(con, days=14):
    out, today_ = [], date.today()
    for c in _contracts(con):
        f = c.get("fields") or {}
        signed = str(c.get("signedAt") or "")[:10]
        addr = str(f.get("address") or "")[:80]
        try:
            sd = date.fromisoformat(signed) if signed else None
        except ValueError:
            sd = None
        if sd and crm.num(f.get("emdDays")) and c.get("status") in ("Signed", "Sent"):
            due = _biz_days(sd, int(crm.num(f["emdDays"])))
            out.append({"kind": "EMD due", "date": due.isoformat(), "address": addr, "amount": crm.num(f.get("emd") or f.get("origEmd")),
                        "contract": c.get("id"), "type": c.get("type")})
        close = f.get("closing")
        if not close and sd and crm.num(f.get("closingDays")):
            close = (sd + timedelta(days=int(crm.num(f["closingDays"])))).isoformat()
        if close:
            out.append({"kind": "Closing", "date": str(close)[:10], "address": addr, "contract": c.get("id"), "type": c.get("type")})
    for d in crm.items(con, "deals"):
        if d.get("closingDate") and not d.get("archived") and d.get("stage") != "Closed":
            out.append({"kind": "Closing", "date": str(d["closingDate"])[:10], "address": str(d.get("address") or "")[:80], "deal": d.get("id")})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["date"]):
        k = (x["kind"], x["date"], x["address"])
        try:
            days_ = (date.fromisoformat(x["date"]) - today_).days
        except ValueError:
            continue  # unreadable date on the record; Finance reports only dates it can read
        if k not in seen:
            seen.add(k)
            uniq.append(dict(x, days=days_))
    return [x for x in uniq if x["days"] <= days]


def ex_closer_review(con, cfg, t):
    deals = [d for d in crm.items(con, "deals") if not d.get("archived") and d.get("stage") in crm.SIGNED]
    issues = []
    for d in deals:
        prob = []
        if d.get("stage") == "Under Contract" and not crm.match_buyers(con, d["id"]):
            prob.append("no matching buyer in your list")
        if not crm.num(d.get("fee")):
            prob.append("no target assignment fee")
        if d.get("stage") in ("Buyer Secured", "Closing Scheduled") and not d.get("closingDate"):
            prob.append("no closing date recorded")
        if prob:
            issues.append({"deal": d["id"], "address": d.get("address"), "stage": d.get("stage"), "issues": prob})
    dl = [x for x in deadlines(con, 10) if x["kind"] == "Closing"]
    s = f"{len(deals)} contracted deal(s); {len(issues)} with blockers; {len(dl)} closing(s) in the next 10 days."
    return _ok(s, {"issues": issues, "closings": dl, "deals": len(deals)})


def ex_dispo_match(con, cfg, t):
    p = t["params"]
    ids = [p["deal_id"]] if p.get("deal_id") else [d["id"] for d in crm.items(con, "deals")
                                                    if not d.get("archived") and d.get("stage") in ("Under Contract", "Buyer Secured")]
    buyers = crm.items(con, "buyers")
    out = []
    for i in ids:
        d = crm.get(con, "deals", i)
        if not d:
            continue
        m = crm.match_buyers(con, i)
        out.append({"deal": i, "address": d.get("address"), "matches": len([x for x in m if x["fit"] == "match"]),
                    "partial": len([x for x in m if x["fit"] == "partial"]), "top": m[:5]})
    weak = [x for x in out if not x["matches"]]
    s = (f"Matched {len(out)} deal(s) against {len(buyers)} buyer(s); {len(weak)} with no full match." if out
         else f"No deals under contract to match yet ({len(buyers)} buyers on file).")
    return _ok(s, {"deals": out, "buyers": len(buyers)}, "Matching uses your buyers' saved markets and max prices only; unknowns are flagged.")


def ex_followup_due(con, cfg, t):
    q = crm.queue(con)
    t0 = crm.today()
    over = [d for d in q if d.get("nextFollowUp") and d["nextFollowUp"] < t0]
    tasks = [x for x in crm.items(con, "tasks") if not x.get("done") and x.get("due") and x["due"] <= t0]
    s = f"{len(q)} lead(s) due for a call today ({len(over)} overdue); {len(tasks)} open task(s) due."
    return _ok(s, {"due": [crm.compact(d) for d in q[:40]], "overdue": len(over), "tasks": tasks[:20]})


def ex_followup_health(con, cfg, t):
    from . import outreach
    oc = outreach.connect()
    ov = outreach.overview(oc, cfg)
    ov.pop("hours", None)
    s = (f"Sent {ov['sent_today']} today, {ov['replies_today']} replies; 7-day response rate {ov['response_rate_7d']}%; "
         f"{len(ov['campaigns'])} campaign(s) live; {ov['queued_today']} queued.")
    if ov.get("gate"):
        s += f" Sending is held: {ov['gate']}"
    return _ok(s, ov)


def ex_followup_launch(con, cfg, t):
    from . import outreach
    cid = t["params"].get("campaign_id")
    if not cid:
        e = ValueError("No campaign given.")
        e.permanent = True
        raise e
    oc = outreach.connect()
    r = outreach.launch(oc, cfg, cid)
    return _ok(f"Launched campaign: {r['activated']} queued, {r['blocked']} excluded.", r)


def ex_frontdesk(con, cfg, t):
    from . import agent
    agent._ensure_tables(con)
    unanswered = con.execute("""SELECT m.phone, MAX(m.id) last_in FROM messages m WHERE m.direction='in'
                               GROUP BY m.phone HAVING last_in > COALESCE((SELECT MAX(o.id) FROM messages o WHERE o.phone=m.phone AND o.direction='out'),0)""").fetchall()
    drafts = con.execute("SELECT COUNT(*) FROM suggestions WHERE state='open'").fetchone()[0]
    hot = con.execute("SELECT COUNT(*) FROM contacts WHERE lead_status='hot'").fetchone()[0]
    stops = con.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND at>=? AND lower(body) LIKE '%stop%'",
                        ((datetime.now() - timedelta(days=1)).isoformat(),)).fetchone()[0]
    s = f"{len(unanswered)} conversation(s) waiting on a reply; {drafts} reply draft(s) ready to send; {hot} hot lead(s); {stops} opt-out(s) in 24h."
    return _ok(s, {"waiting": [r["phone"] for r in unanswered][:50], "drafts": drafts, "hot": hot, "optouts_24h": stops})


def ex_finance(con, cfg, t):
    ov = crm.overview(con)
    dl = deadlines(con, 14)
    flags = []
    offers = [o for o in crm.items(con, "offers") if o.get("status") == "pending_approval"]
    out = {"goal": ov["goal"], "realized": ov["collectedThisMonth"], "projected_signed": ov["projectedNotEarned"],
           "remaining": ov["remaining"], "avg_fee": ov["avgFee"], "avg_fee_source": ov["avgFeeSource"], "deadlines": dl,
           "offers_pending_approval": len(offers)}
    for x in dl:
        if x["days"] < 0:
            flags.append(f"{x['kind']} for {x['address']} was {-x['days']} day(s) ago — confirm it happened")
        elif x["days"] <= 3:
            flags.append(f"{x['kind']} for {x['address']} in {x['days']} day(s)")
    try:
        from . import outreach
        if outreach.twilio_ready()[0]:
            u = outreach.usage(outreach.connect())
            if "balance" in u:
                out["twilio_balance"] = u["balance"]
                if u["balance"] < 5:
                    flags.append(f"Twilio balance is ${u['balance']:.2f}: texts and calls will fail until you add funds")
            month = next((i for i in u.get("items", []) if i["category"] == "totalprice"), None)
            if month:
                out["twilio_month_cost"] = month["price"]
        else:
            out["twilio"] = "not configured"
    except Exception as e:  # noqa: BLE001
        out["twilio_error"] = str(e)[:160]
    out["flags"] = flags
    s = (f"Realized ${ov['collectedThisMonth']:,.0f} of ${ov['goal']:,.0f} this month; ${ov['projectedNotEarned']:,.0f} projected from signed deals (not earned). "
         f"{len(dl)} deadline(s) in 14 days; {len(flags)} flag(s).")
    return _ok(s, out)


# ---------------------------------------------------------------- JARVIS: goals, plans, briefings
DEFAULT_RATES = {"close_per_contract": 0.7, "contract_per_offer": 0.2, "offer_per_conversation": 0.25}


def _rates(con):
    deals = crm.items(con, "deals")
    contracted = [d for d in deals if d.get("stage") in crm.SIGNED + ["Closed"]]
    offered = [d for d in deals if d.get("stage") in ["Offer Made"] + crm.SIGNED + ["Closed"]]
    closed = [d for d in deals if d.get("stage") == "Closed"]
    rates, notes = dict(DEFAULT_RATES), []
    if len(contracted) >= 5:
        rates["close_per_contract"] = max(0.05, len(closed) / len(contracted))
        notes.append(f"close rate from your {len(contracted)} contracted deals")
    if len(offered) >= 10:
        rates["contract_per_offer"] = max(0.02, len(contracted) / len(offered))
        notes.append(f"offer-to-contract rate from your {len(offered)} offers")
    if not notes:
        notes.append("not enough history yet: industry-style starting assumptions (70% of contracts close, 1 in 5 offers signs, 1 in 4 real conversations gets an offer)")
    return rates, notes


def build_plan(con, target, period=None):
    period = period or date.today().strftime("%Y-%m")
    ov = crm.overview(con)
    realized = ov["collectedThisMonth"]
    remaining = max(0.0, float(target) - realized)
    s = crm.settings(con)
    avg = ov["avgFee"] or s.get("defaultFee") or 10000
    avg_src = ov["avgFeeSource"] if ov["avgFee"] else "your default fee setting"
    rates, notes = _rates(con)
    signed = [d for d in crm.items(con, "deals") if not d.get("archived") and d.get("stage") in crm.SIGNED]
    expected_from_signed = len(signed) * rates["close_per_contract"]
    deals_needed = max(0, math.ceil(remaining / avg - expected_from_signed)) if avg else None
    contracts = math.ceil(deals_needed / rates["close_per_contract"]) if deals_needed else 0
    offers = math.ceil(contracts / rates["contract_per_offer"]) if contracts else 0
    convos = math.ceil(offers / rates["offer_per_conversation"]) if offers else 0
    today_ = date.today()
    last = (date(today_.year + (today_.month == 12), today_.month % 12 + 1, 1) - timedelta(days=1))
    workdays = sum(1 for i in range((last - today_).days + 1) if (today_ + timedelta(days=i)).weekday() < 5) or 1
    return {"target": float(target), "period": period, "realized": realized, "remaining": remaining,
            "avg_fee": avg, "avg_fee_source": avg_src, "signed_deals": len(signed), "expected_closings_from_signed": round(expected_from_signed, 1),
            "deals_needed": deals_needed, "contracts_needed": contracts, "offers_needed": offers, "conversations_needed": convos,
            "workdays_left": workdays,
            "per_day": {"conversations": math.ceil(convos / workdays) if convos else 0, "offers": math.ceil(offers / workdays) if offers else 0},
            "rates": rates, "assumptions": notes,
            "disclaimer": "A plan, not a promise: targets come from your numbers and stated assumptions. Realized revenue only counts closed deals with collected fees."}


def set_goal(con, target, text="", period=None):
    target = float(target)
    if target <= 0:
        raise ValueError("Give a positive monthly target.")
    period = period or date.today().strftime("%Y-%m")
    con.execute("UPDATE hq_goals SET status='replaced' WHERE status='active' AND period=?", (period,))
    gid = "g_" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO hq_goals(id,text,target,period,status,created) VALUES(?,?,?,?,?,?)",
                (gid, (text or f"${target:,.0f} in assignment fees for {period}")[:300], target, period, "active", now()))
    con.commit()
    t = create_task(con, "jarvis.plan", {"goal_id": gid}, goal_id=gid, priority=1, source="owner",
                    objective=f"Plan for ${target:,.0f} in {period}")
    return {"goal_id": gid, "task": t}


def active_goal(con):
    r = con.execute("SELECT * FROM hq_goals WHERE status='active' ORDER BY created DESC LIMIT 1").fetchone()
    if not r:
        return None
    g = dict(r)
    g["plan"] = json.loads(g["plan"]) if g["plan"] else None
    return g


def _daily_tasks(con, goal_id, parent, day, plan=None):
    """Today's assignments. Dedupe keys make this safe to call twice."""
    lim = max(10, min(100, (plan or {}).get("per_day", {}).get("conversations", 25) * 2))
    spec = [("hunter.top_leads", {"tiers": ["A", "B"], "limit": lim}, 2),
            ("hunter.review_backlog", {}, 4),
            ("followup.due_list", {}, 2),
            ("followup.campaign_health", {}, 3),
            ("frontdesk.triage", {}, 2),
            ("dispo.match_buyers", {}, 3),
            ("closer.pipeline_review", {}, 3),
            ("finance.check", {}, 3)]
    ids = []
    for kind, params, pri in spec:
        t = create_task(con, kind, params, goal_id=goal_id, parent_id=parent, priority=pri, source="jarvis",
                        dedupe_key=f"{kind}:{day}", objective="Assigned by JARVIS for today's plan")
        ids.append(t["id"])
    return ids


def ex_jarvis_plan(con, cfg, t):
    gid = t["params"].get("goal_id")
    g = con.execute("SELECT * FROM hq_goals WHERE id=?", (gid,)).fetchone()
    if not g:
        e = ValueError("Goal not found.")
        e.permanent = True
        raise e
    plan = build_plan(con, g["target"], g["period"])
    day = date.today().isoformat()
    kids = _daily_tasks(con, gid, t["id"], day, plan)
    plan["assigned_task_ids"] = kids
    con.execute("UPDATE hq_goals SET plan=? WHERE id=?", (_j(plan), gid))
    rid = "r_" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO hq_reports(id,kind,at,title,body,task_id) VALUES(?,?,?,?,?,?)",
                (rid, "plan", now(), f"Plan: ${g['target']:,.0f} for {g['period']}", _j(plan), t["id"]))
    s = (f"Need ${plan['remaining']:,.0f} more → about {plan['deals_needed']} more closings → {plan['contracts_needed']} contracts → "
         f"{plan['offers_needed']} offers → {plan['conversations_needed']} real seller conversations "
         f"(~{plan['per_day']['conversations']}/workday). Assigned {len(kids)} tasks.")
    return _ok(s, plan, "; ".join(plan["assumptions"]))


def ex_jarvis_briefing(con, cfg, t):
    day = t["params"].get("day") or date.today().isoformat()
    g = active_goal(con)
    kids = _daily_tasks(con, g["id"] if g else None, t["id"], day, (g or {}).get("plan"))
    rep = create_task(con, "jarvis.briefing_report", {"day": day, "tasks": kids}, depends_on=kids, priority=2, source="jarvis",
                      dedupe_key=f"jarvis.briefing_report:{day}", goal_id=g["id"] if g else None, parent_id=t["id"])
    return _ok(f"Assigned {len(kids)} tasks for {day}; the briefing is written when they finish.", {"tasks": kids, "report_task": rep["id"]})


def _compose(con, ids, title, kind, task_id):
    rows = [get_task(con, i, events=False) for i in ids]
    g = active_goal(con)
    ov = crm.overview(con)
    lines = [{"agent": AGENTS[r["agent"]]["name"], "task": r["title"], "status": r["status"], "summary": r.get("summary") or r.get("error") or ""} for r in rows]
    pending = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='awaiting_approval'").fetchone()[0]
    fin = next((r for r in rows if r["kind"] == "finance.check" and r["status"] == "completed"), None)
    body = {"goal": (g or {}).get("target"), "realized": ov["collectedThisMonth"], "projected": ov["projectedNotEarned"],
            "lines": lines, "approvals_waiting": pending,
            "flags": ((fin or {}).get("result") or {}).get("flags", []) if fin else [],
            "failed": [x for x in lines if x["status"] in ("failed", "blocked")]}
    rid = "r_" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO hq_reports(id,kind,at,title,body,task_id) VALUES(?,?,?,?,?,?)", (rid, kind, now(), title, _j(body), task_id))
    con.commit()
    return body


def ex_jarvis_report(con, cfg, t):
    ids = t["params"].get("tasks") or []
    body = _compose(con, ids, f"Morning briefing · {t['params'].get('day')}", "briefing", t["id"])
    s = f"Briefing ready: {sum(1 for x in body['lines'] if x['status'] == 'completed')}/{len(ids)} tasks done, {len(body['flags'])} flag(s), {body['approvals_waiting']} approval(s) waiting."
    return _ok(s, body)


def ex_jarvis_eod(con, cfg, t):
    day = t["params"].get("day") or date.today().isoformat()
    ids = [r["id"] for r in con.execute("SELECT id FROM hq_tasks WHERE substr(created,1,10)=? AND kind NOT LIKE 'jarvis.%'", (day,))]
    body = _compose(con, ids, f"End of day · {day}", "eod", t["id"])
    open_ = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status IN ('queued','waiting','running')").fetchone()[0]
    body["still_open"] = open_
    s = (f"Today: {sum(1 for x in body['lines'] if x['status'] == 'completed')} task(s) completed, {len(body['failed'])} failed/blocked, "
         f"{open_} still open, {body['approvals_waiting']} approval(s) waiting.")
    return _ok(s, body)


EXECUTORS = {
    "jarvis.plan": ex_jarvis_plan, "jarvis.briefing": ex_jarvis_briefing, "jarvis.briefing_report": ex_jarvis_report,
    "jarvis.eod": ex_jarvis_eod, "hunter.sync_sources": ex_hunter_sync, "hunter.top_leads": ex_hunter_top,
    "hunter.review_backlog": ex_hunter_review, "closer.pipeline_review": ex_closer_review, "dispo.match_buyers": ex_dispo_match,
    "followup.due_list": ex_followup_due, "followup.campaign_health": ex_followup_health, "followup.launch_campaign": ex_followup_launch,
    "frontdesk.triage": ex_frontdesk, "finance.check": ex_finance,
    **agents_ai.EXECUTORS,
}


# ---------------------------------------------------------------- schedule (durable: keyed by day, survives restarts)
def schedule_tick(con, cfg, at_dt=None):
    hq = cfg.get("hq") or {}
    if hq.get("enabled") is False or paused(con):
        return []
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    at_dt = at_dt or datetime.now(tz)
    day, hm = at_dt.strftime("%Y-%m-%d"), at_dt.strftime("%H:%M")
    made = []
    if at_dt.weekday() < 5 or hq.get("weekends"):
        if hm >= hq.get("briefing_time", "08:00"):
            made.append(create_task(con, "jarvis.briefing", {"day": day}, source="schedule", priority=1, dedupe_key=f"jarvis.briefing:{day}")["id"])
        if hm >= hq.get("eod_time", "18:00"):
            made.append(create_task(con, "jarvis.eod", {"day": day}, source="schedule", priority=2, dedupe_key=f"jarvis.eod:{day}")["id"])
    if hq.get("duties", True):  # Stage 2: every agent's standing job (durable, keyed by day/week)
        duties.tick(con, cfg, at_dt)
    return made


def tick(con, cfg, log=print):
    schedule_tick(con, cfg)
    return run_due(con, cfg, log=log)


# ---------------------------------------------------------------- read models for the HQ
def agent_status(con):
    out = {}
    since = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    for aid, a in AGENTS.items():
        cnt = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM hq_tasks WHERE agent=? GROUP BY status", (aid,))}
        running = con.execute("SELECT id,title,started FROM hq_tasks WHERE agent=? AND status='running' ORDER BY started LIMIT 1", (aid,)).fetchone()
        last = con.execute("SELECT id,title,status,summary,error,finished,updated FROM hq_tasks WHERE agent=? AND status IN ('completed','failed','blocked') ORDER BY updated DESC LIMIT 1", (aid,)).fetchone()
        ev = con.execute("SELECT at,message FROM hq_events WHERE agent=? ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
        operational = any(v[0] == aid for v in KINDS.values())
        if not operational:
            state = "not_operational"
        elif running:
            state = "working"
        elif cnt.get("awaiting_approval"):
            state = "awaiting_approval"
        elif last and last["status"] in ("failed", "blocked") and last["updated"] >= (datetime.now() - timedelta(hours=24)).isoformat():
            state = last["status"]
        elif cnt.get("queued"):
            state = "queued"
        elif cnt.get("waiting"):
            state = "waiting"
        else:
            state = "idle"
        sc = con.execute("""SELECT SUM(status='completed' AND verified=1) done, SUM(status='failed') failed, SUM(status='blocked') blocked,
                            SUM(status='completed' AND (deadline IS NULL OR finished<=deadline)) ontime,
                            AVG(CASE WHEN status='completed' THEN (julianday(finished)-julianday(started))*86400 END) secs
                            FROM hq_tasks WHERE agent=? AND updated>=?""", (aid, since)).fetchone()
        done, failed = sc["done"] or 0, sc["failed"] or 0
        if state == "idle" and aid in duties.MONITORING:
            state = "idle_monitoring"  # nothing to do right now, but it has a standing job and is watching for work
        out[aid] = dict(a, id=aid, state=state, operational=operational, division_name=org.DIVISIONS[a["division"]]["name"],
                        duties=duties.board(con).get(aid, []), monitoring=duties.MONITORING.get(aid, ""),
                        budget=duties.budgets(con).get(aid), spent_today=round(duties.spent_by(con, aid), 4),
                        task_types=[k for k, v in KINDS.items() if v[0] == aid], queue=cnt.get("queued", 0) + cnt.get("waiting", 0),
                        approvals=cnt.get("awaiting_approval", 0), running=dict(running) if running else None,
                        last=dict(last) if last else None, last_event=dict(ev) if ev else None,
                        scorecard={"completed_7d": done, "failed_7d": failed, "blocked_7d": sc["blocked"] or 0,
                                   "success_rate": round(100 * done / (done + failed), 0) if done + failed else None,
                                   "avg_seconds": round(sc["secs"] or 0, 1)})
    return out


def engine_online(con, minutes=3):
    t = db.kv_get(con, "hq_last_tick")
    try:
        return t is not None and (datetime.now() - datetime.fromisoformat(t[:19])).total_seconds() < minutes * 60
    except ValueError:
        return False


def state(con, cfg):
    g = active_goal(con)
    rep = con.execute("SELECT * FROM hq_reports ORDER BY at DESC LIMIT 1").fetchone()
    ags = agent_status(con)
    if not engine_online(con):  # the PC or JARVIS Engine is off: nobody is working, whatever the last task said
        for a in ags.values():
            if a.get("operational"):
                a["state"] = "offline"
    return {"agents": ags, "goal": g, "engine_online": engine_online(con), "plan_preview": None if g else build_plan(con, crm.settings(con)["goal"]),
            "approvals": list_tasks(con, statuses=["awaiting_approval"], limit=50),
            "events": [dict(e) for e in con.execute("SELECT task_id,agent,at,type,message FROM hq_events ORDER BY id DESC LIMIT 40")],
            "counts": {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM hq_tasks GROUP BY status")},
            "deadlines": deadlines(con, 14), "latest_report": (dict(rep, body=json.loads(rep["body"])) if rep else None),
            "kinds": {k: {"agent": v[0], "level": v[1], "title": v[2], "division": AGENTS[v[0]]["division"]} for k, v in KINDS.items()}, "now": now(),
            "divisions": org.DIVISIONS,
            "by_division": {f"{r['division']}|{r['status']}": r["n"] for r in con.execute("SELECT division, status, COUNT(*) n FROM hq_tasks GROUP BY division, status")},
            "worker_loop": db.kv_get(con, "hq_last_tick"), "hunter_leads": _hunter_dots(con), "health": health(con),
            "ai": agents_ai.usage(con), "reviews": agents_ai.pending_reviews(con)}


def health(con):
    """Worker health from the database itself: loop heartbeat, queue depth, oldest waiting task, live leases."""
    q = con.execute("SELECT COUNT(*) n, MIN(created) oldest FROM hq_tasks WHERE status='queued'").fetchone()
    return {"last_tick": db.kv_get(con, "hq_last_tick"), "paused": paused(con), "paused_at": db.kv_get(con, "hq_paused") or None,
            "queued": q["n"], "oldest_queued": q["oldest"],
            "running": [dict(r) for r in con.execute("SELECT id,agent,title,started,lease_until FROM hq_tasks WHERE status='running'")],
            "awaiting_approval": con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='awaiting_approval'").fetchone()[0]}


def since(con, ts, limit=40):
    """What happened while the owner was away, straight from the task table (only verified completions count as done)."""
    ts = str(ts or "")[:19]
    if not ts:
        raise ValueError("Missing time.")
    rows = lambda sql, *a: [dict(r) for r in con.execute(sql, (ts, *a))]  # noqa: E731
    done = rows("SELECT id,agent,title,summary,finished FROM hq_tasks WHERE status='completed' AND verified=1 AND finished>=? ORDER BY finished DESC LIMIT ?", limit)
    bad = rows("SELECT id,agent,title,status,error,recovery,updated FROM hq_tasks WHERE status IN ('failed','blocked') AND updated>=? ORDER BY updated DESC LIMIT ?", limit)
    appr = rows("SELECT id,agent,title,created FROM hq_tasks WHERE status='awaiting_approval' AND created>=? ORDER BY created DESC LIMIT ?", limit)
    reps = rows("SELECT id,kind,title,at FROM hq_reports WHERE at>=? ORDER BY at DESC LIMIT 5")
    ran = con.execute("SELECT COUNT(*) FROM hq_events WHERE type='started' AND at>=?", (ts,)).fetchone()[0]
    return {"since": ts, "completed": done, "problems": bad, "new_approvals": appr, "reports": reps, "attempts": ran,
            "still_waiting": con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='awaiting_approval'").fetchone()[0],
            "health": health(con)}


def _hunter_dots(con):
    r = con.execute("SELECT result FROM hq_tasks WHERE kind='hunter.top_leads' AND status='completed' ORDER BY finished DESC LIMIT 1").fetchone()
    try:
        return [{"address": x.get("address"), "tier": x.get("tier")} for x in (json.loads(r["result"]) or {}).get("leads", [])[:40]] if r else []
    except (ValueError, AttributeError):
        return []


def reports(con, limit=20):
    return [dict(r, body=json.loads(r["body"])) for r in con.execute("SELECT * FROM hq_reports ORDER BY at DESC LIMIT ?", (int(limit),))]


def chat_log(con, agent_id, role, text, task_id=None):
    if agent_id not in AGENTS:
        raise ValueError("Unknown agent.")
    con.execute("INSERT INTO hq_chats(agent,at,role,text,task_id) VALUES(?,?,?,?,?)", (agent_id, now(), role[:12], str(text)[:2000], task_id))
    con.commit()
    return {"ok": True}


def chats(con, agent_id, limit=30):
    rows = [dict(r) for r in con.execute("SELECT agent,at,role,text,task_id FROM hq_chats WHERE agent=? ORDER BY id DESC LIMIT ?", (agent_id, int(limit)))]
    return list(reversed(rows))


# ---------------------------------------------------------------- outreach email (AI Sidekicks SDR), sent by the app through your Gmail
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[A-Za-z]{2,}$")


def email_dnc(con):
    return set(json.loads(db.kv_get(con, "email_dnc") or "[]"))


def email_bounced(con):
    return {r[0] for r in con.execute("SELECT email FROM hq_email_bounces")}


def email_bounce_add(con, address, message_id="", email_task="", reason="", at=None):
    """A send bounced: no follow-ups or new sends to that address, and anything queued to it is cancelled."""
    a = str(address or "").strip().lower()
    if not EMAIL_RE.match(a):
        raise ValueError("That isn't a valid email address.")
    new = not con.execute("SELECT 1 FROM hq_email_bounces WHERE email=?", (a,)).fetchone()
    con.execute("INSERT OR IGNORE INTO hq_email_bounces(email,at,message_id,email_task,reason) VALUES(?,?,?,?,?)",
                (a, str(at or now())[:40], str(message_id or "")[:200], str(email_task or "")[:40], str(reason or "")[:300]))
    for r in con.execute("SELECT id, params FROM hq_tasks WHERE kind IN ('sdr.send_email','sdr.followup_draft') AND status IN ('queued','waiting','awaiting_approval')").fetchall():
        if (json.loads(r["params"] or "{}").get("to") or "").lower() == a:
            cancel(con, r["id"], by="bounce")
    if new and email_task:
        event(con, email_task, "sdr", "bounce", f"Email to {a} bounced")
    con.commit()
    return {"email": a, "new": new}


def email_params(con, p):
    """Exactly what will be sent. Checked when the task is created, so what you approve is what goes out."""
    to = str(p.get("to") or "").strip().lower()
    subject = str(p.get("subject") or "").strip()
    body = str(p.get("body") or "").strip()
    if not EMAIL_RE.match(to):
        raise ValueError("That isn't a valid email address.")
    if to in email_dnc(con):
        raise ValueError(f"{to} asked not to be emailed. It's on your do-not-email list.")
    bounced = con.execute("SELECT at FROM hq_email_bounces WHERE email=?", (to,)).fetchone()
    if bounced:
        raise ValueError(f"Email to {to} bounced on {str(bounced['at'])[:10]}; it isn't a working address.")
    if not subject or len(subject) > 200:
        raise ValueError("The email needs a subject (up to 200 characters).")
    if not body or len(body) > 8000:
        raise ValueError("The email needs a body (up to 8,000 characters).")
    ph = PLACEHOLDER_RE.search(subject + "\n" + body)
    if ph:
        raise ValueError(f"The email still has a placeholder ({ph.group(0)[:40]}). Fill it in or remove it before sending.")
    claims = agents_ai.claims_found(subject + "\n" + _strip_sig(body))
    if claims:
        raise ValueError("The email makes a claim we can't back up: " + "; ".join(claims) + ". Reword it before sending.")
    out = {"to": to, "subject": subject, "body": _strip_sig(body)}
    sig = p.get("signature") if isinstance(p.get("signature"), dict) and p["signature"].get("name") and p["signature"].get("address") else di_signature(con)
    if sig:  # added to every AI Sidekicks email (text and HTML with the logo); part of what you approve
        out["signature"] = sig
    for k in ("prospect_id", "company", "draft_task", "thread_id", "campaign_id"):
        if p.get(k):
            out[k] = str(p[k])[:120]
    step = int(p.get("step") or 1)
    if not 1 <= step <= 3:
        raise ValueError("Outreach stops after the first email and two follow-ups.")
    out["step"] = step
    return out


def ex_sdr_send(con, cfg, t):
    # nothing to do on the PC: the approved email waits here until the app sends it from your Gmail and reports back
    return {"state": "wait", "next_in": 86400, "note": "Approved. Waiting for the JARVIS app to send it from your Gmail."}


EXECUTORS["sdr.send_email"] = ex_sdr_send
EXECUTORS.update(duties.EXECUTORS)


def _approved_email(con, tid):
    t = get_task(con, tid, events=False)
    if t["kind"] != "sdr.send_email":
        raise ValueError("That isn't an outreach email.")
    if t["approval_state"] != "approved" or t.get("approved_hash") != t["params_hash"]:
        raise ValueError("That email hasn't been approved for these exact words.")
    if t["status"] in TERMINAL:
        raise ValueError("That email is already " + t["status"] + ".")
    if t["params"].get("to") in email_dnc(con):
        raise ValueError("That address is on your do-not-email list now. Not sending.")
    return t


def email_outbox(con):
    rows = con.execute("SELECT id FROM hq_tasks WHERE kind='sdr.send_email' AND approval_state='approved' AND status IN ('queued','waiting','running') ORDER BY approved_at").fetchall()
    out = []
    for r in rows:
        t = get_task(con, r["id"], events=False)
        if t.get("approved_hash") == t["params_hash"]:
            out.append({"id": t["id"], "params": t["params"], "approved_at": t.get("approved_at"), "note": t.get("verify_note") or ""})
    return {"outbox": out}


def email_sent(con, tid, message_id, thread_id=None):
    t = _approved_email(con, tid)
    if not str(message_id or "").strip():
        raise ValueError("Gmail didn't return a message id, so this can't be marked sent.")
    res = {"message_id": str(message_id)[:200], "thread_id": str(thread_id or "")[:200], "to": t["params"]["to"], "subject": t["params"]["subject"], "sent_at": now()}
    _set(con, tid, status="completed", lease_owner=None, lease_until=None, finished=now(), result=_j(res),
         summary=f"Sent to {t['params']['to']}: {t['params']['subject']}"[:600], verified=1, verify_note="Gmail returned a message id.", error=None)
    event(con, tid, t["agent"], "completed", f"Sent from your Gmail to {t['params']['to']}", {"message_id": res["message_id"]})
    con.commit()
    return get_task(con, tid, events=False)


def email_failed(con, tid, error):
    t = _approved_email(con, tid)
    note = "Gmail didn't send it: " + str(error or "unknown error")[:300]
    _set(con, tid, verify_note=note)
    event(con, tid, t["agent"], "send_failed", note)
    con.commit()
    return get_task(con, tid, events=False)


def email_dnc_add(con, address):
    a = str(address or "").strip().lower()
    if not EMAIL_RE.match(a):
        raise ValueError("That isn't a valid email address.")
    cur = email_dnc(con); cur.add(a)
    db.kv_set(con, "email_dnc", json.dumps(sorted(cur)))
    # anything already queued to that address stops here
    for r in con.execute("SELECT id, params FROM hq_tasks WHERE kind IN ('sdr.send_email','ae.book_meeting') AND status IN ('queued','waiting','awaiting_approval')").fetchall():
        pp = json.loads(r["params"] or "{}")
        if (pp.get("to") or pp.get("attendee") or "") == a:
            cancel(con, r["id"], by="do-not-email")
    con.commit()
    return {"email_dnc": sorted(cur)}



# ---------------------------------------------------------------- Phase 10: replies, follow-ups, meetings, agreements, demos
REPLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_email_replies(id INTEGER PRIMARY KEY, email_task TEXT, prospect_id TEXT, message_id TEXT UNIQUE,
  from_addr TEXT, at TEXT, text TEXT, class TEXT, classify_task TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS hq_email_replies_p ON hq_email_replies(prospect_id);
CREATE TABLE IF NOT EXISTS hq_email_bounces(email TEXT PRIMARY KEY, at TEXT, message_id TEXT, email_task TEXT, reason TEXT);
"""
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery (?:subsystem|system)", re.I)
BOUNCE_TEXT = re.compile(r"address not found|couldn'?t be delivered|could not be delivered|undeliverable|delivery (?:status notification|has failed|failed)|"
                         r"user unknown|no such user|mailbox (?:unavailable|not found|does not exist)|recipient address rejected|550[ -]5\.\d\.\d", re.I)
# Reference only, never a promise: Hunter, "State of Email Outreach 2026" (2025 data, 31 million emails, hunter.io/the-state-of-cold-email).
REPLY_BENCHMARK = {"average_reply_rate": 4.5, "three_message_sequences": 6.8, "single_message": 3.3,
                   "source": "Hunter, State of Email Outreach 2026 (2025 data from 31M emails by its users)",
                   "note": "One vendor's users; a reference point, not what to expect. Under about 100 sends, zero replies isn't evidence yet."}
YES = ("interested", "meeting")
FOLLOWUP_DAYS = {2: 3, 3: 7}  # follow-up #2 three days after the first email, #3 seven days after it
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _sent_emails(con, prospect_id=None):
    rows = con.execute("SELECT id FROM hq_tasks WHERE kind='sdr.send_email' AND status='completed' AND verified=1 ORDER BY finished").fetchall()
    out = []
    for r in rows:
        t = get_task(con, r["id"], events=False)
        p, res = t["params"], (t.get("result") or {})
        if prospect_id and p.get("prospect_id") != prospect_id:
            continue
        out.append({"id": t["id"], "prospect_id": p.get("prospect_id"), "company": p.get("company"), "to": p.get("to"), "subject": p.get("subject"),
                    "body": p.get("body"), "step": int(p.get("step") or 1), "sent_at": res.get("sent_at") or t.get("finished"),
                    "message_id": res.get("message_id"), "thread_id": res.get("thread_id") or p.get("thread_id")})
    return out


def email_history(con, prospect_id=None, days=60):
    cutoff = (datetime.now() - timedelta(days=int(days or 60))).isoformat(timespec="seconds")
    return {"sent": [e for e in _sent_emails(con, prospect_id) if (e["sent_at"] or "") >= cutoff]}


def email_reply_add(con, email_task, message_id, from_addr, at, text):
    """The app found a reply in Gmail. Recorded once per Gmail message; the SDR reads it (classify) on the next run."""
    t = get_task(con, email_task, events=False)
    if t["kind"] != "sdr.send_email" or t["status"] != "completed":
        raise ValueError("That isn't a sent outreach email.")
    mid, frm = str(message_id or "").strip()[:200], str(from_addr or "").strip().lower()[:200]
    if not mid:
        raise ValueError("A reply needs Gmail's message id.")
    if frm != (t["params"].get("to") or "").lower():
        raise ValueError("That message isn't from the person the email went to.")
    cur = con.execute("SELECT * FROM hq_email_replies WHERE message_id=?", (mid,)).fetchone()
    if cur:
        return {"reply": dict(cur), "new": False}
    body = str(text or "").strip()[:6000]
    con.execute("INSERT INTO hq_email_replies(email_task,prospect_id,message_id,from_addr,at,text,created) VALUES(?,?,?,?,?,?,?)",
                (email_task, t["params"].get("prospect_id"), mid, frm, str(at or now())[:40], body, now()))
    rid = con.execute("SELECT id FROM hq_email_replies WHERE message_id=?", (mid,)).fetchone()["id"]
    ct = create_task(con, "sdr.reply_classify", {"reply_id": rid, "company": t["params"].get("company") or "", "reply_text": body or "(empty reply)",
                                                 "our_subject": t["params"].get("subject"), "our_email": t["params"].get("body", "")[:2000]},
                     title=f"Read the reply from {t['params'].get('company') or frm}"[:150], division="demigod", source="gmail",
                     dedupe_key=f"reply:{mid}")
    con.execute("UPDATE hq_email_replies SET classify_task=? WHERE id=?", (ct["id"], rid))
    event(con, email_task, "sdr", "reply", f"Reply received from {frm}")
    con.commit()
    return {"reply": dict(con.execute("SELECT * FROM hq_email_replies WHERE id=?", (rid,)).fetchone()), "new": True}


def email_replies(con, prospect_id=None, limit=100):
    q, a = "SELECT * FROM hq_email_replies", []
    if prospect_id:
        q, a = q + " WHERE prospect_id=?", [prospect_id]
    rows = [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", a + [int(limit)])]
    return {"replies": rows, "email_dnc": sorted(email_dnc(con)), "email_bounced": sorted(email_bounced(con))}


def _classify_then_apply(con, cfg, t):
    out = agents_ai.EXECUTORS["sdr.reply_classify"](con, cfg, t)
    if out.get("verified"):
        o = ((out.get("result") or {}).get("output") or {})
        cls = str(o.get("class") or "other").strip().lower()
        if cls not in ("interested", "meeting", "question", "not_now", "unsubscribe", "out_of_office", "other"):
            cls = "other"
        rid = (t["params"] if isinstance(t["params"], dict) else json.loads(t["params"] or "{}")).get("reply_id")
        row = con.execute("SELECT * FROM hq_email_replies WHERE id=?", (rid,)).fetchone()
        if row:
            con.execute("UPDATE hq_email_replies SET class=? WHERE id=?", (cls, rid))
            if cls == "unsubscribe":  # they asked to stop: honoured right away, no approval needed to stop contacting someone
                email_dnc_add(con, row["from_addr"])
            con.commit()
        out["summary"] = f"Reply read: {cls.replace('_', ' ')}. " + str(out.get("summary") or "")[:400]
    return out


def said_yes(con, prospect_id):
    if not prospect_id:
        return False
    r = con.execute(f"SELECT 1 FROM hq_email_replies WHERE prospect_id=? AND class IN ({','.join('?' * len(YES))}) LIMIT 1", (prospect_id, *YES)).fetchone()
    if r:
        return True
    for row in con.execute("SELECT params FROM hq_tasks WHERE kind='ae.book_meeting' AND status='completed'"):
        if json.loads(row["params"] or "{}").get("prospect_id") == prospect_id:
            return True
    return False


def demo_gate(con, prospect_id):
    if not said_yes(con, prospect_id):
        raise ValueError("Demos are only built for companies that said yes: a reply read as interested or a meeting, or a booked call.")


def followups_due(con, today_=None):
    """Who's due for follow-up #2 or #3: no reply, not on do-not-email, and enough days since the first email."""
    day = today_ or datetime.now()
    dnc = email_dnc(con) | email_bounced(con)  # stop on any reply (auto-replies included), an opt-out or a bounce
    replied = {r["prospect_id"] for r in con.execute("SELECT prospect_id FROM hq_email_replies") if r["prospect_id"]}
    by = {}
    for e in _sent_emails(con):
        if e["prospect_id"]:
            by.setdefault(e["prospect_id"], []).append(e)
    pending = set()
    for r in con.execute("SELECT params FROM hq_tasks WHERE kind IN ('sdr.send_email','sdr.followup_draft') AND status IN ('queued','running','waiting','awaiting_approval')"):
        pid = json.loads(r["params"] or "{}").get("prospect_id") or json.loads(r["params"] or "{}").get("recordId")
        if pid:
            pending.add(pid)
    due = []
    for pid, es in by.items():
        if pid in replied or pid in pending or (es[-1]["to"] or "") in dnc:
            continue
        first = next((e for e in es if e["step"] == 1), es[0])
        step = max(e["step"] for e in es) + 1
        if step > 3:
            continue
        try:
            age = (day - datetime.fromisoformat(first["sent_at"][:19])).days
        except (TypeError, ValueError):
            continue
        if age >= FOLLOWUP_DAYS[step]:
            due.append({"prospect_id": pid, "company": es[-1]["company"], "to": es[-1]["to"], "step": step, "days": age,
                        "thread_id": es[-1]["thread_id"], "previous": [{"subject": e["subject"], "body": e["body"][:1500], "sent_at": e["sent_at"]} for e in es]})
    return {"due": sorted(due, key=lambda x: -x["days"])}


def meeting_params(con, p):
    to = str(p.get("attendee") or "").strip().lower()
    if not EMAIL_RE.match(to):
        raise ValueError("The meeting needs the guest's email address.")
    if to in email_dnc(con):
        raise ValueError(f"{to} is on your do-not-email list.")
    start, end = str(p.get("start") or ""), str(p.get("end") or "")
    if not ISO_RE.match(start) or not ISO_RE.match(end) or end <= start:
        raise ValueError("The meeting needs a start and an end time (local time, like 2026-11-10T14:00).")
    title = str(p.get("title") or "").strip()
    if not title or len(title) > 150:
        raise ValueError("The meeting needs a title.")
    out = {"attendee": to, "name": str(p.get("name") or "")[:120], "start": start[:19], "end": end[:19], "title": title,
           "description": str(p.get("description") or "")[:3000], "meet": bool(p.get("meet", True))}
    for k in ("prospect_id", "company"):
        if p.get(k):
            out[k] = str(p[k])[:120]
    return out


def agreement_params(con, p):
    to = str(p.get("signer_email") or "").strip().lower()
    if not EMAIL_RE.match(to):
        raise ValueError("The agreement needs the signer's email address.")
    name = str(p.get("signer_name") or "").strip()
    if not name:
        raise ValueError("The agreement needs the signer's name.")
    for k in ("template_id", "account_id", "role_name"):
        if not str(p.get(k) or "").strip():
            raise ValueError("Pick your DocuSign agreement template (and its signer role) in Settings first.")
    out = {"signer_email": to, "signer_name": name[:120], "template_id": str(p["template_id"])[:80], "account_id": str(p["account_id"])[:80],
           "role_name": str(p["role_name"])[:80], "subject": str(p.get("subject") or "Agreement for signature")[:150]}
    tabs = {}
    for k, v in (p.get("tabs") or {}).items():
        if str(k).strip() and str(v).strip():
            tabs[str(k)[:60]] = str(v)[:200]
    out["tabs"] = tabs
    for k in ("prospect_id", "company"):
        if p.get(k):
            out[k] = str(p[k])[:120]
    return out


def demo_params(con, p):
    pid = str(p.get("prospect_id") or "")
    demo_gate(con, pid)
    spec = get_task(con, str(p.get("spec_task") or ""), events=False) if p.get("spec_task") else None
    if not spec or spec["kind"] != "architect.demo_spec" or spec["status"] != "completed" or spec.get("approval_state") != "accepted":
        raise ValueError("Accept the Solutions Architect's demo design first.")
    o = (spec.get("result") or {}).get("output") or {}
    return {"prospect_id": pid, "company": str(p.get("company") or "")[:120], "spec_task": spec["id"],
            "name": str(o.get("agent_name") or f"Demo · {p.get('company')}")[:120], "first_message": str(o.get("first_message") or "")[:1000],
            "prompt": str(o.get("prompt") or "")[:12000]}


ACTION_PARAMS = {"sdr.send_email": email_params, "ae.book_meeting": meeting_params, "ae.send_agreement": agreement_params, "architect.build_demo": demo_params}
for _k in ("ae.book_meeting", "ae.send_agreement", "architect.build_demo"):
    EXECUTORS[_k] = lambda con, cfg, t: {"state": "wait", "next_in": 86400, "note": "Approved. Waiting for the JARVIS app to carry it out with your connector."}
EXECUTORS["sdr.reply_classify"] = _classify_then_apply


def _approved_action(con, tid, kinds):
    t = get_task(con, tid, events=False)
    if t["kind"] not in kinds:
        raise ValueError("That isn't one of these actions.")
    if t["approval_state"] != "approved" or t.get("approved_hash") != t["params_hash"]:
        raise ValueError("That action hasn't been approved with these exact details.")
    if t["status"] in TERMINAL:
        raise ValueError("That action is already " + t["status"] + ".")
    if t["kind"] == "architect.build_demo":
        demo_gate(con, t["params"].get("prospect_id"))
    return t


def action_outbox(con, kinds=None):
    ks = [k for k in (kinds or ["ae.book_meeting", "ae.send_agreement", "architect.build_demo"]) if k in ACTION_PARAMS and k != "sdr.send_email"]
    if not ks:
        return {"outbox": []}
    rows = con.execute(f"SELECT id FROM hq_tasks WHERE kind IN ({','.join('?' * len(ks))}) AND approval_state='approved' AND status IN ('queued','waiting','running') ORDER BY approved_at", ks).fetchall()
    out = []
    for r in rows:
        t = get_task(con, r["id"], events=False)
        if t.get("approved_hash") == t["params_hash"]:
            out.append({"id": t["id"], "kind": t["kind"], "params": t["params"], "approved_at": t.get("approved_at"), "note": t.get("verify_note") or ""})
    return {"outbox": out}


def action_done(con, tid, result):
    t = _approved_action(con, tid, ("ae.book_meeting", "ae.send_agreement", "architect.build_demo"))
    res = result if isinstance(result, dict) else {}
    key = {"ae.book_meeting": "event_id", "ae.send_agreement": "envelope_id", "architect.build_demo": "agent_id"}[t["kind"]]
    if not str(res.get(key) or "").strip():
        raise ValueError(f"The connector didn't return {key.replace('_', ' ')}, so this can't be marked done.")
    clean = {k: str(v)[:500] for k, v in res.items() if k in (key, "link", "status", "html_link", "meet_link")}
    clean["done_at"] = now()
    what = {"ae.book_meeting": f"Meeting booked with {t['params'].get('attendee')}", "ae.send_agreement": f"Agreement sent to {t['params'].get('signer_email')}",
            "architect.build_demo": f"Demo agent built for {t['params'].get('company')}"}[t["kind"]]
    _set(con, tid, status="completed", lease_owner=None, lease_until=None, finished=now(), result=_j(clean), summary=what[:600], verified=1,
         verify_note=f"The connector returned {key.replace('_', ' ')}.", error=None)
    event(con, tid, t["agent"], "completed", what, {key: clean[key]})
    con.commit()
    return get_task(con, tid, events=False)


def action_failed(con, tid, error):
    t = _approved_action(con, tid, ("ae.book_meeting", "ae.send_agreement", "architect.build_demo"))
    note = "The connector didn't do it: " + str(error or "unknown error")[:300]
    _set(con, tid, verify_note=note)
    event(con, tid, t["agent"], "action_failed", note)
    con.commit()
    return get_task(con, tid, events=False)


def prospect_status(con, prospect_id):
    """One prospect's outreach picture for the app: emails, replies, yes/no, actions."""
    acts = []
    for r in con.execute("SELECT id FROM hq_tasks WHERE kind IN ('ae.book_meeting','ae.send_agreement','architect.build_demo') ORDER BY created"):
        t = get_task(con, r["id"], events=False)
        if t["params"].get("prospect_id") == prospect_id:
            acts.append({"id": t["id"], "kind": t["kind"], "status": t["status"], "params": t["params"], "result": t.get("result"), "note": t.get("verify_note") or ""})
    return {"sent": _sent_emails(con, prospect_id), "replies": email_replies(con, prospect_id)["replies"], "said_yes": said_yes(con, prospect_id), "actions": acts}


# ---------------------------------------------------------------- outreach campaigns (AI Sidekicks): many emails, one review, paced sending
CAMPAIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_campaigns(id TEXT PRIMARY KEY, name TEXT, status TEXT, daily_limit INTEGER, footer TEXT, created TEXT,
  approved_at TEXT, approved_hash TEXT, note TEXT, find_tasks TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS hq_campaign_items(id INTEGER PRIMARY KEY, campaign_id TEXT, prospect_id TEXT, company TEXT, to_addr TEXT,
  draft_task TEXT, subject TEXT, body TEXT, status TEXT, email_task TEXT, error TEXT, updated TEXT, tries INTEGER DEFAULT 0, UNIQUE(campaign_id, prospect_id));
"""
CAMPAIGN_MAX = 200
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{2,80}\]|\{\{|\}\}|<<[^>]{1,40}>>|\bTBD\b|\bXX+\b")


def di_offer(con):
    return (db.kv_get(con, "di_offer") or "").strip() or agents_ai.DEFAULT_OFFER


def set_di_offer(con, text):
    t = str(text or "").strip()
    if t and len(t) < 40:
        raise ValueError("Describe what AI Sidekicks offers in at least a sentence or two (or leave it empty for the default).")
    db.kv_set(con, "di_offer", t[:1500]); con.commit()
    return {"offer": di_offer(con), "default": not t}


def _sender(footer):
    return (str(footer or "").split(",")[0] or "the owner").strip()[:80]
OPT_OUT_RE = re.compile(r"no thanks|unsubscribe|opt[ -]?out|won.t (email|follow|reach)|stop (emailing|hearing)|not interested", re.I)


def _camp(con, cid):
    r = con.execute("SELECT * FROM hq_campaigns WHERE id=?", (cid,)).fetchone()
    if not r:
        raise ValueError("That campaign doesn't exist.")
    c = dict(r)
    c["find_tasks"] = json.loads(c.get("find_tasks") or "[]")
    return c


def _ever_emailed(con):
    out = set()
    for r in con.execute("SELECT params FROM hq_tasks WHERE kind='sdr.send_email' AND status NOT IN ('cancelled','failed')"):
        out.add((json.loads(r["params"] or "{}").get("to") or "").lower())
    return out


GMAIL_DAILY_MAX = 500  # Gmail's own limit for a personal account; JARVIS adds no lower cap unless you set one


def campaign_create(con, name, daily_limit, footer, prospects=None, find_tasks=None):
    name = str(name or "").strip()[:120] or "Outreach campaign"
    lim = int(daily_limit or GMAIL_DAILY_MAX)
    if not 1 <= lim <= GMAIL_DAILY_MAX:
        raise ValueError(f"Daily sending limit must be between 1 and {GMAIL_DAILY_MAX} (Gmail's own daily limit).")
    footer = str(footer or "").strip()
    if len(footer) < 12:
        raise ValueError("Commercial email needs your name and a mailing address in the footer. Set it in Settings first.")
    cid = "cmp_" + uuid.uuid4().hex[:10]
    con.execute("INSERT INTO hq_campaigns(id,name,status,daily_limit,footer,created,find_tasks) VALUES(?,?,?,?,?,?,?)",
                (cid, name, "drafting", lim, footer[:400], now(), _j([str(x)[:40] for x in (find_tasks or [])][:20])))
    con.commit()
    res = campaign_add(con, cid, prospects or [])
    return dict(res, id=cid)


def campaign_add(con, cid, prospects):
    c = _camp(con, cid)
    if c["status"] not in ("drafting", "review"):
        raise ValueError("Prospects can only be added before the campaign is approved.")
    have = con.execute("SELECT COUNT(*) FROM hq_campaign_items WHERE campaign_id=?", (cid,)).fetchone()[0]
    dnc, sent = email_dnc(con), _ever_emailed(con)
    added, skipped = 0, []
    for p in (prospects or [])[:CAMPAIGN_MAX]:
        pid, to = str(p.get("prospect_id") or "")[:60], str(p.get("to") or "").strip().lower()
        co = str(p.get("company") or "")[:120]
        why = ("no prospect id" if not pid else "no valid email" if not EMAIL_RE.match(to) else "on do-not-email" if to in dnc
               else "already emailed" if to in sent else "campaign is full" if have + added >= CAMPAIGN_MAX else "")
        if not why and con.execute("SELECT 1 FROM hq_campaign_items WHERE campaign_id=? AND (prospect_id=? OR to_addr=?)", (cid, pid, to)).fetchone():
            why = "already in this campaign"
        if why:
            skipped.append({"company": co, "why": why})
            continue
        snap = p.get("snapshot") if isinstance(p.get("snapshot"), dict) else {}
        params = dict({k: v for k, v in snap.items() if k not in ("campaign_id",)}, recordKind="prospect", recordId=pid, company=co or snap.get("company") or to,
                      campaign_id=cid, sender=_sender(c["footer"]), offer=di_offer(con))
        t = create_task(con, "sdr.campaign_email", params, title=f"Campaign email · {co or to}"[:150], division="demigod", source="campaign",
                        dedupe_key=f"campaign:{cid}:{pid}", priority=3)
        info = p.get("info") if isinstance(p.get("info"), dict) else None
        con.execute("INSERT INTO hq_campaign_items(campaign_id,prospect_id,company,to_addr,draft_task,status,updated,info) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, pid, co, to, t["id"], "drafting", now(), _j(info) if info else None))
        added += 1
    con.execute("UPDATE hq_campaigns SET status='drafting' WHERE id=? AND status='review'", (cid,)) if added else None
    con.commit()
    return {"added": added, "skipped": skipped}


DI_SUBJECT = "Custom AI Assistants & CRMs for {company}"
DI_SUBJECT_FALLBACK = "Custom AI Assistants & CRMs for your team"


def di_subject(company):
    """Mola's house subject line for AI Sidekicks intro emails, with the company's short name (no LLC/Inc./parentheses)."""
    c = re.sub(r"\s*\([^)]*\)", "", str(company or ""))
    c = re.sub(r",?\s+(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Co\.)$", "", c.strip(), flags=re.I).strip(" ,.")
    s = DI_SUBJECT.format(company=c) if c else DI_SUBJECT_FALLBACK
    return s if len(s) <= 90 else DI_SUBJECT_FALLBACK


def _pick_email(out):
    o = out or {}
    if o.get("subject") and o.get("body"):
        return str(o["subject"]).strip()[:200], str(o["body"]).strip()[:6000]
    for d in o.get("drafts") or []:
        if re.search(r"mail", str(d.get("channel") or "email"), re.I) and d.get("subject") and d.get("body"):
            return str(d["subject"]).strip()[:200], str(d["body"]).strip()[:6000]
    return None


def _items_hash(items):
    ready = sorted(((i["id"], i["to_addr"], i["subject"] or "", i["body"] or "") for i in items if i["status"] == "ready"))
    return hashlib.sha256(json.dumps(ready).encode()).hexdigest()[:24]


def _today_local(cfg):
    return datetime.now(ZoneInfo((cfg or {}).get("timezone", "America/New_York")))


def campaign_get(con, cid, cfg=None):
    c = _camp(con, cid)
    cur = email_footer(con)
    if cur and c["status"] in ("drafting", "review") and cur != c["footer"]:  # you changed the footer before approving: use the new one
        con.execute("UPDATE hq_campaigns SET footer=? WHERE id=?", (cur, cid)); con.commit()
        c["footer"] = cur
    for it in [dict(r) for r in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=?", (cid,))]:
        if it["status"] == "drafting":
            t = get_task(con, it["draft_task"], events=False)
            if t["status"] == "completed" and t.get("verified"):
                em = _pick_email((t.get("result") or {}).get("output"))
                if em:
                    em = (di_subject(it["company"]), re.sub(r"\bgives? (?:it|the team|your team) (?:its |their )?time back\b", "give them their time back", em[1]))  # house subject + Mola's wording
                    ph = PLACEHOLDER_RE.search(em[0] + "\n" + em[1])
                    con.execute("UPDATE hq_campaign_items SET subject=?, body=?, status=?, error=?, updated=? WHERE id=?",
                                (em[0], em[1], "needs_edit" if ph else "ready", f"Has a placeholder: {ph.group(0)[:40]}" if ph else None, now(), it["id"]))
                else:
                    con.execute("UPDATE hq_campaign_items SET status='failed', error=?, updated=? WHERE id=?", ("The draft had no email in it.", now(), it["id"]))
            elif t["status"] in ("failed", "cancelled", "blocked"):
                con.execute("UPDATE hq_campaign_items SET status='failed', error=?, updated=? WHERE id=?", ((t.get("error") or t["status"])[:300], now(), it["id"]))
        elif it["status"] == "ready" and PLACEHOLDER_RE.search((it["subject"] or "") + "\n" + (it["body"] or "")):
            ph = PLACEHOLDER_RE.search((it["subject"] or "") + "\n" + (it["body"] or ""))
            con.execute("UPDATE hq_campaign_items SET status='needs_edit', error=?, updated=? WHERE id=?", (f"Has a placeholder: {ph.group(0)[:40]}", now(), it["id"]))
        elif it["status"] in ("approved", "sending") and it["email_task"]:
            t = get_task(con, it["email_task"], events=False)
            stale = it["status"] == "sending" and (it["updated"] or "") < (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
            if t["status"] == "completed":
                con.execute("UPDATE hq_campaign_items SET status='sent', updated=? WHERE id=?", (now(), it["id"]))
            elif t["status"] == "cancelled":
                con.execute("UPDATE hq_campaign_items SET status='cancelled', error=?, updated=? WHERE id=?", ("Cancelled (do-not-email or campaign cancelled).", now(), it["id"]))
            elif stale and not str(t.get("verify_note") or "").startswith("Gmail didn't send it"):
                # claimed but never reported back: Gmail may well have sent it, so never resend on a guess
                con.execute("UPDATE hq_campaign_items SET status='unconfirmed', error=?, updated=? WHERE id=?",
                            ("Sending started but JARVIS never got Gmail's confirmation. Check your Sent folder before sending it again.", now(), it["id"]))
            elif stale:  # the app reported that Gmail refused it: safe to retry, up to 3 tries
                st = "failed" if (it["tries"] or 0) >= 3 else "approved"
                con.execute("UPDATE hq_campaign_items SET status=?, error=?, updated=? WHERE id=?", (st, (t.get("verify_note") or "The send wasn't confirmed.")[:300], now(), it["id"]))
                if st == "failed":
                    try:
                        cancel(con, t["id"], by="campaign: 3 failed tries")
                    except ValueError:
                        pass
    items = [dict(r) for r in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=? ORDER BY id", (cid,))]
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    if c["status"] == "drafting" and items and not counts.get("drafting"):
        con.execute("UPDATE hq_campaigns SET status='review' WHERE id=?", (cid,)); c["status"] = "review"
    if c["status"] == "sending" and items and not counts.get("approved") and not counts.get("sending"):
        con.execute("UPDATE hq_campaigns SET status='done' WHERE id=?", (cid,)); c["status"] = "done"
    con.commit()
    finds = []
    for ft in c["find_tasks"]:
        try:
            t = get_task(con, ft, events=False)
            finds.append({"id": t["id"], "status": t["status"], "approval_state": t.get("approval_state"), "title": t["title"],
                          "found": len(((t.get("result") or {}).get("output") or {}).get("prospects") or [])})
        except ValueError:
            pass
    return {"campaign": c, "items": items, "counts": counts, "items_hash": _items_hash(items), "finds": finds, "stats": campaign_stats(con, cid),
            "signature": di_signature(con),
            "sent_today": _sent_today(con, cfg), "window": _send_window(cfg)}


def campaigns(con):
    out = []
    for r in con.execute("SELECT id FROM hq_campaigns ORDER BY created DESC LIMIT 30"):
        g = campaign_get(con, r["id"])
        out.append({"id": g["campaign"]["id"], "name": g["campaign"]["name"], "status": g["campaign"]["status"], "daily_limit": g["campaign"]["daily_limit"],
                    "created": g["campaign"]["created"], "counts": g["counts"], "total": len(g["items"]), "stats": g["stats"]})
    return {"campaigns": out}


def campaign_edit_item(con, cid, item_id, subject=None, body=None, drop=False):
    c = _camp(con, cid)
    if c["status"] not in ("drafting", "review"):
        raise ValueError("Emails can only be changed before the campaign is approved.")
    it = con.execute("SELECT * FROM hq_campaign_items WHERE id=? AND campaign_id=?", (int(item_id), cid)).fetchone()
    if not it:
        raise ValueError("That email isn't in this campaign.")
    if drop:
        con.execute("UPDATE hq_campaign_items SET status='dropped', updated=? WHERE id=?", (now(), it["id"]))
    else:
        if it["status"] not in ("ready", "dropped", "needs_edit"):
            raise ValueError("That email isn't drafted yet.")
        sj, bd = str(subject or "").strip(), str(body or "").strip()
        if not sj or len(sj) > 200 or not bd or len(bd) > 6000:
            raise ValueError("Each email needs a subject (up to 200 characters) and a body.")
        ph = PLACEHOLDER_RE.search(sj + "\n" + bd)
        if ph:
            raise ValueError(f"That still has a placeholder ({ph.group(0)[:40]}). Fill it in or remove it.")
        con.execute("UPDATE hq_campaign_items SET subject=?, body=?, status='ready', updated=? WHERE id=?", (sj, bd, now(), it["id"]))
    con.commit()
    return campaign_get(con, cid)


def campaign_redraft(con, cid, include_ready=False):
    """Have the SDR write the failed and placeholder emails again (or every email, e.g. after you change the pitch)."""
    c = _camp(con, cid)
    if c["status"] not in ("drafting", "review"):
        raise ValueError("Emails can only be redrafted before the campaign is approved.")
    campaign_get(con, cid)
    sts = ("failed", "needs_edit", "ready") if include_ready else ("failed", "needs_edit")
    rows = con.execute(f"SELECT * FROM hq_campaign_items WHERE campaign_id=? AND status IN ({','.join('?' * len(sts))})", (cid, *sts)).fetchall()
    if not rows:
        raise ValueError("Nothing to redraft: every email is ready, dropped or still drafting.")
    n = 0
    for it in rows:
        info = json.loads(it["info"] or "{}") if it["info"] else {}
        old = get_task(con, it["draft_task"], events=False)["params"] if it["draft_task"] else {}
        params = {k: v for k, v in dict(old, **{k: v for k, v in info.items() if v}).items() if k not in ("email", "area")}
        params.update(recordKind="prospect", recordId=it["prospect_id"], company=it["company"] or params.get("company") or it["to_addr"], campaign_id=cid,
                      sender=_sender(c["footer"]), offer=di_offer(con))
        t = create_task(con, "sdr.campaign_email", params, title=f"Campaign email · {it['company'] or it['to_addr']}"[:150], division="demigod", source="campaign",
                        dedupe_key=f"campaign:{cid}:{it['prospect_id']}:r{uuid.uuid4().hex[:6]}", priority=3)
        con.execute("UPDATE hq_campaign_items SET draft_task=?, status='drafting', error=NULL, subject=NULL, body=NULL, updated=? WHERE id=?", (t["id"], now(), it["id"]))
        n += 1
    con.execute("UPDATE hq_campaigns SET status='drafting' WHERE id=?", (cid,))
    con.commit()
    return {"redrafting": n}


def campaign_approve(con, cid, items_hash, cfg=None):
    """Your one approval for the whole batch, bound to the exact emails you reviewed. Each becomes its own approved email task."""
    g = campaign_get(con, cid, cfg)
    c = g["campaign"]
    if c["status"] != "review":
        raise ValueError("The campaign isn't ready for approval (still drafting, or already approved).")
    if items_hash != g["items_hash"]:
        raise ValueError("The emails changed since you reviewed them. Review them again before approving.")
    ready = [i for i in g["items"] if i["status"] == "ready"]
    fix = [i for i in g["items"] if i["status"] in ("needs_edit", "failed")]
    if fix:
        raise ValueError(f"{len(fix)} email{'s' if len(fix) > 1 else ''} still {'have' if len(fix) > 1 else 'has'} a placeholder or failed to draft. Redraft, edit or drop {'them' if len(fix) > 1 else 'it'} first.")
    if not ready:
        raise ValueError("There are no emails ready to send.")
    made, skipped = 0, 0
    for i in ready:
        body = i["body"]
        if not OPT_OUT_RE.search(body):
            body += "\n\nIf this isn't relevant, just reply \"no thanks\" and I won't email again."
        try:
            t = create_task(con, "sdr.send_email", {"to": i["to_addr"], "subject": i["subject"], "body": body, "prospect_id": i["prospect_id"],
                                                     "company": i["company"], "draft_task": i["draft_task"], "campaign_id": cid, "step": 1,
                                                     "signature": di_signature(con) or _sig_from_footer(c["footer"])},
                            title=f"Campaign email · {i['company'] or i['to_addr']}"[:150], division="demigod", source="campaign")
            approve(con, t["id"], t["params_hash"], by="owner (campaign approval)")
            con.execute("UPDATE hq_campaign_items SET status='approved', email_task=?, updated=? WHERE id=?", (t["id"], now(), i["id"]))
            made += 1
        except ValueError as e:
            con.execute("UPDATE hq_campaign_items SET status='skipped', error=?, updated=? WHERE id=?", (str(e)[:300], now(), i["id"]))
            skipped += 1
    con.execute("UPDATE hq_campaigns SET status='sending', approved_at=?, approved_hash=? WHERE id=?", (now(), items_hash, cid))
    con.commit()
    return {"approved": made, "skipped": skipped}


def _send_window(cfg):
    """Campaign emails go out on business hours only (config: outreach_window {days: [0-6, Mon=0], start: 9, end: 17})."""
    w = (cfg or {}).get("outreach_window") or {}
    days, start, end = [int(x) for x in w.get("days", [0, 1, 2, 3, 4])], int(w.get("start", 9)), int(w.get("end", 17))
    d = _today_local(cfg)
    names = "weekdays" if days == [0, 1, 2, 3, 4] else "every day" if sorted(days) == list(range(7)) else ",".join("MTWTFSS"[x] for x in days)
    fmt = lambda h: "midnight" if h in (0, 24) else f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"
    rule = "any time" if sorted(days) == list(range(7)) and start <= 0 and end >= 24 else f"{names} {fmt(start)}-{fmt(end)}"
    return {"open": d.weekday() in days and start <= d.hour < end, "rule": rule, "now": d.strftime("%a %H:%M")}


def _sent_today(con, cfg=None):
    day = datetime.now().strftime("%Y-%m-%d")
    n = 0
    for r in con.execute("SELECT result FROM hq_tasks WHERE kind='sdr.send_email' AND status='completed' AND finished>=?", (day,)):
        n += 1
    return n


def campaign_due(con, cfg=None):
    """What the app may send right now: approved campaign emails, inside the send window, under today's limit (all outreach counts)."""
    win = _send_window(cfg)
    sent = _sent_today(con, cfg)
    due = []
    if win["open"]:
        for r in con.execute("SELECT * FROM hq_campaigns WHERE status='sending' ORDER BY approved_at"):
            room = int(r["daily_limit"]) - sent - len(due)
            if room <= 0:
                continue
            for it in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=? AND status='approved' ORDER BY id LIMIT ?", (r["id"], room)):
                t = get_task(con, it["email_task"], events=False)
                if t["status"] in ("queued", "waiting", "running") and t["approval_state"] == "approved" and t.get("approved_hash") == t["params_hash"]:
                    due.append({"campaign_id": r["id"], "item_id": it["id"], "task_id": t["id"], "company": it["company"], "to": it["to_addr"]})
    return {"due": due, "window": win, "sent_today": sent, "engine_sender": engine_sender(con)["active"]}


def campaign_claim(con, task_id, cfg=None):
    """The app is about to send this one. Only something campaign_due lists right now can be claimed, once."""
    if not any(d["task_id"] == task_id for d in campaign_due(con, cfg)["due"]):
        raise ValueError("That email isn't due to send right now (window, daily limit, paused or already sending).")
    cur = con.execute("UPDATE hq_campaign_items SET status='sending', tries=COALESCE(tries,0)+1, updated=? WHERE email_task=? AND status='approved'", (now(), task_id))
    con.commit()
    if cur.rowcount != 1:
        raise ValueError("That email is already being sent.")
    return {"claimed": task_id}


def campaign_set(con, cid, action, daily_limit=None):
    c = _camp(con, cid)
    if action == "limit":
        lim = int(daily_limit or GMAIL_DAILY_MAX)
        if not 1 <= lim <= GMAIL_DAILY_MAX:
            raise ValueError(f"Daily sending limit must be between 1 and {GMAIL_DAILY_MAX} (Gmail's own daily limit).")
        con.execute("UPDATE hq_campaigns SET daily_limit=? WHERE id=?", (lim, cid))
    elif action == "pause":
        if c["status"] != "sending":
            raise ValueError("Only a sending campaign can be paused.")
        con.execute("UPDATE hq_campaigns SET status='paused' WHERE id=?", (cid,))
    elif action == "resume":
        if c["status"] != "paused":
            raise ValueError("That campaign isn't paused.")
        con.execute("UPDATE hq_campaigns SET status='sending' WHERE id=?", (cid,))
    elif action == "cancel":
        if c["status"] in ("done", "cancelled"):
            raise ValueError("That campaign is already finished.")
        for it in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=? AND status='unconfirmed'", (cid,)).fetchall():
            try:
                cancel(con, it["email_task"], by="campaign cancelled")  # it may have gone out, so the item keeps saying 'unconfirmed'
            except ValueError:
                pass
        for it in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=? AND status IN ('approved','drafting','ready','sending')", (cid,)).fetchall():
            for tid in (it["email_task"], it["draft_task"] if it["status"] == "drafting" else None):
                if tid:
                    try:
                        cancel(con, tid, by="campaign cancelled")
                    except ValueError:
                        pass
            con.execute("UPDATE hq_campaign_items SET status='cancelled', updated=? WHERE id=?", (now(), it["id"]))
        con.execute("UPDATE hq_campaigns SET status='cancelled' WHERE id=?", (cid,))
    else:
        raise ValueError("Unknown campaign action.")
    con.commit()
    return campaign_get(con, cid)


# ---------------------------------------------------------------- prospect searches (voice: "tell the SDR to find 100 property managers in MA")
SEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_prospect_searches(id TEXT PRIMARY KEY, area TEXT, count INTEGER, find_tasks TEXT, created TEXT, campaign_id TEXT, source TEXT);
"""
SEARCH_CHUNK = 25


def search_start(con, area, count, source="owner", industry=None):
    """One request, done in chunks of up to 25 that run one after another, each told what the earlier ones already found."""
    area = str(area or "").strip()[:120]
    if not area:
        raise ValueError("Which city, county or state should the SDR search?")
    n = max(1, min(100, int(count or 25)))
    sid = "srch_" + uuid.uuid4().hex[:10]
    parts = -(-n // SEARCH_CHUNK)
    tasks, prev = [], None
    for k in range(parts):
        cnt = min(SEARCH_CHUNK, n - k * SEARCH_CHUNK)
        t = create_task(con, "sdr.find_prospects", {"area": area, "count": cnt, "search_id": sid, "part": f"{k + 1} of {parts}",
                                                   **({"industry": duties.industry_phrase(industry)} if industry else {}),
                                                   "note": "Other parts of this search find other companies: spread across different cities and towns in the area and skip everything in already_found."},
                        title=f"Find prospects · {(industry + ' · ') if industry else ''}{area}" + (f" (part {k + 1} of {parts})" if parts > 1 else ""), division="demigod", source=source,
                        depends_on=[prev] if prev else None, priority=3)
        tasks.append(t["id"]); prev = t["id"]
    con.execute("INSERT INTO hq_prospect_searches(id,area,count,find_tasks,created,source,industry) VALUES(?,?,?,?,?,?,?)",
                (sid, area, n, _j(tasks), now(), source, (industry or "property management").strip().lower()))
    con.commit()
    return search_get(con, sid)


def _co_key(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())[:60]


def _search_found(con, s):
    found, seen = [], set()
    ft = s["find_tasks"]
    for tid in (json.loads(ft or "[]") if isinstance(ft, str) or ft is None else ft):
        t = get_task(con, tid, events=False)
        if t["status"] != "completed":
            continue
        for r in ((t.get("result") or {}).get("output") or {}).get("prospects") or []:
            key = (str(r.get("company") or "").lower().strip(), str(r.get("email") or "").lower().strip())
            if key[0] and key not in seen:
                seen.add(key); found.append(dict(r))
    # emails the SDR looked up afterwards, one company at a time
    et = s.get("enrich_tasks")
    looked = {}
    for tid in (json.loads(et or "[]") if isinstance(et, str) or et is None else et):
        try:
            t = get_task(con, tid, events=False)
        except ValueError:
            continue
        if t["status"] != "completed":
            continue
        o = (t.get("result") or {}).get("output") or {}
        em = str(o.get("email") or "").strip().lower()
        if EMAIL_RE.match(em) and str(o.get("email_source") or "").startswith("http"):
            looked[t["params"].get("key")] = (em, o["email_source"], o.get("contact_page") or "", o.get("phone") or "")
    for r in found:
        hit = looked.get(_co_key(r.get("company")))
        if hit and not EMAIL_RE.match(str(r.get("email") or "").strip().lower()):
            r["email"], r["email_source"] = hit[0], hit[1]
            r["phone"] = r.get("phone") or hit[3]
            r["notes"] = (str(r.get("notes") or "") + f" Email found on {hit[1]}").strip()[:300]
    return found


def search_get(con, sid):
    r = con.execute("SELECT * FROM hq_prospect_searches WHERE id=?", (sid,)).fetchone()
    if not r:
        raise ValueError("That search doesn't exist.")
    s = dict(r)
    sts = [get_task(con, t, events=False)["status"] for t in json.loads(s["find_tasks"] or "[]")]
    found = _search_found(con, s)
    s.update(find_tasks=json.loads(s["find_tasks"] or "[]"), parts=len(sts), parts_done=sum(1 for x in sts if x == "completed"),
             parts_failed=sum(1 for x in sts if x in ("failed", "cancelled", "blocked")), found=len(found),
             with_email=sum(1 for x in found if EMAIL_RE.match(str(x.get("email") or "").strip().lower())))
    s["done"] = s["parts_done"] + s["parts_failed"] == s["parts"]
    ets = json.loads(s.get("enrich_tasks") or "[]") if isinstance(s.get("enrich_tasks"), str) or s.get("enrich_tasks") is None else s["enrich_tasks"]
    est = [get_task(con, t, events=False)["status"] for t in ets]
    s.update(enrich_tasks=ets, lookups=len(est), lookups_done=sum(1 for x in est if x in ("completed", "failed", "cancelled", "blocked")),
             emails_from_lookup=sum(1 for x in found if x.get("email_source")))
    s["lookup_running"] = bool(est) and s["lookups_done"] < len(est)
    return s


def search_enrich(con, sid, source="owner"):
    """Look up a published contact email for every company in a finished search that doesn't have one (3 web searches each, at most)."""
    s = search_get(con, sid)
    if not s["done"]:
        raise ValueError(f"The SDR is still searching {s['area']} ({s['parts_done']} of {s['parts']} parts).")
    if s["lookup_running"]:
        raise ValueError(f"The SDR is already looking up emails ({s['lookups_done']} of {s['lookups']} done).")
    have = set()
    for tid in s["enrich_tasks"]:
        try:
            have.add(get_task(con, tid, events=False)["params"].get("key"))
        except ValueError:
            pass
    todo = [r for r in _search_found(con, s) if not EMAIL_RE.match(str(r.get("email") or "").strip().lower()) and _co_key(r.get("company")) not in have][:150]
    if not todo:
        raise ValueError("Every company in that search already has an email, or was already looked up.")
    made = []
    for r in todo:
        k = _co_key(r.get("company"))
        t = create_task(con, "sdr.find_contact", {"company": str(r.get("company"))[:120], "website": str(r.get("website") or "")[:300], "city": str(r.get("city") or "")[:80],
                                                  "state": str(r.get("state") or "")[:40], "listing": str(r.get("source") or "")[:300], "search_id": sid, "key": k},
                        title=f"Find email · {r.get('company')}"[:150], division="demigod", source=source, dedupe_key=f"contact:{sid}:{k}", priority=4)
        made.append(t["id"])
    con.execute("UPDATE hq_prospect_searches SET enrich_tasks=? WHERE id=?", (_j(s["enrich_tasks"] + made), sid))
    con.commit()
    return {"queued": len(made), "max_cost": round(len(made) * 0.045, 2), "search": search_get(con, sid)}


def searches(con, limit=10):
    return {"searches": [search_get(con, r["id"]) for r in con.execute("SELECT id FROM hq_prospect_searches ORDER BY created DESC LIMIT ?", (int(limit),))]}


def _find_with_context(orig):
    def run(con, cfg, t):
        p = json.loads(t["params"]) if isinstance(t.get("params"), str) else dict(t.get("params") or {})
        if p.get("search_id"):
            r = con.execute("SELECT * FROM hq_prospect_searches WHERE id=?", (p["search_id"],)).fetchone()
            if r:
                p["already_found"] = [x.get("company") for x in _search_found(con, dict(r))][:150]
                t = dict(t, params=p)
        return orig(con, cfg, t)
    return run


EXECUTORS["sdr.find_prospects"] = _find_with_context(agents_ai.EXECUTORS["sdr.find_prospects"])


def _save_to_crm(kind):
    """Email lookups and research queued for an AI Sidekicks CRM business (params.dc_business_id) save what they found onto that record."""
    orig = agents_ai.EXECUTORS[kind]

    def run(con, cfg, t):
        out = orig(con, cfg, t)
        p = t["params"] if isinstance(t.get("params"), dict) else json.loads(t.get("params") or "{}")
        bid = p.get("dc_business_id")
        if bid and out.get("verified"):
            from . import dcrm
            try:
                con.executescript(dcrm.SCHEMA)
                r = dcrm.apply_sdr_result(con, bid, kind, ((out.get("result") or {}).get("output") or {}))
                out["note"] = (out.get("note") or "") + f"; saved to the CRM ({', '.join(r['saved']) or 'nothing new'}; now {r['score']} {r['priority']})"
                out.setdefault("result", {})["crm"] = r
            except Exception as e:  # noqa: BLE001  (the lookup itself still counts; the CRM save is reported)
                out["note"] = (out.get("note") or "") + f"; couldn't save to the CRM: {e}"
        return out
    return run


for _k in ("sdr.find_contact", "sdr.research_prospect"):
    EXECUTORS[_k] = _save_to_crm(_k)


def di_record_brief(con, prospect_id=None, business_id=None, limit=10):
    """The CRM's sourced facts about a business, compact, for drafts (follow-ups, proposals, demo pages). {} if it isn't in the CRM."""
    from . import dcrm
    con.executescript(dcrm.SCHEMA)
    bid = business_id
    if not bid and prospect_id:
        r = con.execute("SELECT business_id FROM dc_links WHERE prospect_id=?", (prospect_id,)).fetchone()
        bid = r[0] if r else None
    if not bid:
        return {}
    b = con.execute("SELECT * FROM dc_businesses WHERE id=?", (bid,)).fetchone()
    if not b:
        return {}
    facts = [{"fact": f["value"], "source": f["source_url"]} for f in con.execute(
        "SELECT field, value, source_url FROM dc_facts WHERE business_id=? AND field IN ('research','note','about','inquiries','operations','services','size','software','chat') ORDER BY id DESC LIMIT ?",
        (bid, int(limit)))]
    people = [{"name": c["name"], "title": c["title"]} for c in con.execute("SELECT name, title FROM dc_contacts WHERE business_id=? AND name IS NOT NULL AND name != ''", (bid,))]
    return {"business_id": bid, "name": b["name"], "industry": b["industry"], "city": b["city"], "services": b["services"], "size": b["size"],
            "website": b["website"], "people": people[:4], "facts": facts}


def di_pricing(con):
    return {"pricing_guide": db.kv_get(con, "di_pricing") or ""}


def set_di_pricing(con, text):
    """Your price ranges (setup and monthly) for the proposal drafts. Empty clears it; drafts then suggest a range for you to confirm."""
    t = str(text or "").strip()
    if len(t) > 1500:
        raise ValueError("Keep the pricing guide under 1,500 characters.")
    db.kv_set(con, "di_pricing", t); con.commit()
    return di_pricing(con)


def _with_deal_context(orig):
    """A proposal draft gets the CRM record, the pre-call brief and your pricing guide, so it works from facts, not guesses."""
    def run(con, cfg, t):
        p = dict(t["params"] if isinstance(t.get("params"), dict) else json.loads(t.get("params") or "{}"))
        p["record"] = di_record_brief(con, prospect_id=p.get("prospect_id"), business_id=p.get("dc_business_id"), limit=15)
        try:
            from . import precall
            b = precall.get(con, p.get("prospect_id") or p.get("company") or "")
            if b and b.get("brief"):
                p["precall_brief"] = {k: b["brief"].get(k) for k in ("who_they_are", "size", "software", "current_setup", "likely_pain", "reviews") if b["brief"].get(k)}
        except Exception:  # noqa: BLE001
            pass
        p["pricing_guide"] = db.kv_get(con, "di_pricing") or ""
        return orig(con, cfg, dict(t, params=p))
    return run


EXECUTORS["ae.discovery_proposal"] = _with_deal_context(agents_ai.EXECUTORS["ae.discovery_proposal"])


def di_proposal(con, company=None, notes=None, dc_business_id=None, prospect_id=None, source="owner"):
    """Start a proposal draft from discovery call notes (or a call transcript). Level 2: it waits for your review; nothing is sent."""
    n = str(notes or "").strip()
    if len(n) < 40:
        raise ValueError("Paste the call notes or the transcript (at least a few sentences) so the proposal has something real to work from.")
    name = str(company or "").strip()
    if dc_business_id and not name:
        r = con.execute("SELECT name FROM dc_businesses WHERE id=?", (dc_business_id,)).fetchone()
        name = r[0] if r else ""
    if not name:
        raise ValueError("Which company is the proposal for?")
    params = {"company": name[:120], "notes": n[:20000]}
    if dc_business_id:
        params["dc_business_id"] = str(dc_business_id)
    if prospect_id:
        params["prospect_id"] = str(prospect_id)
    return create_task(con, "ae.discovery_proposal", params, title=f"Proposal · {name}"[:150], division="demigod", source=source, priority=2)


def di_research(con, ids=None, priority="Research required", limit=25, source="owner"):
    """Queue the SDR to research AI Sidekicks CRM businesses: a published email (if none on record) and sourced facts (if none yet).
    Results save onto each record by themselves. Each lookup is about $0.04-0.05 of AI spend, under the SDR's daily budget."""
    from . import dcrm
    con.executescript(dcrm.SCHEMA)
    if ids:
        rows = [con.execute("SELECT * FROM dc_businesses WHERE id=?", (i,)).fetchone() for i in ids]
        rows = [r for r in rows if r]
    else:
        rows = con.execute("""SELECT * FROM dc_businesses WHERE stage NOT IN ('Won','Lost','Disqualified') AND (?='' OR priority=?)
                              ORDER BY created LIMIT ?""", (priority or "", priority or "", max(1, min(int(limit or 25), 100)))).fetchall()
    made, skipped = [], 0
    for r in rows:
        b = dict(r)
        has_email = con.execute("SELECT 1 FROM dc_contacts WHERE business_id=? AND email IS NOT NULL AND email_class IN ('verified','public_unverified','third_party') AND suppressed=0", (b["id"],)).fetchone()
        has_facts = con.execute("SELECT 1 FROM dc_facts WHERE business_id=? AND field='research'", (b["id"],)).fetchone()
        base = {"company": b["name"], "city": b.get("city") or "", "state": b.get("state") or "", "phone": b.get("phone") or "",
                "website": b.get("website") or "", "industry": b.get("industry") or "", "dc_business_id": b["id"]}
        src = con.execute("SELECT source_url FROM dc_facts WHERE business_id=? AND source_url LIKE 'http%' ORDER BY id LIMIT 1", (b["id"],)).fetchone()
        if src:
            base["listing"] = src[0]
        for kind, need, label in (("sdr.research_prospect", not has_facts, "Research"), ("sdr.find_contact", not has_email, "Find email")):
            if not need:
                continue
            key = f"{kind}:{b['id']}"
            before = con.execute("SELECT 1 FROM hq_tasks WHERE dedupe_key=?", (key,)).fetchone()
            t = create_task(con, kind, base, title=f"{label} · {b['name']}"[:150], division="demigod", source=source, dedupe_key=key, priority=3)
            if before:
                skipped += 1
            else:
                made.append(t["id"])
    return {"queued": len(made), "already_queued": skipped, "businesses": len(rows), "max_cost": round(len(made) * 0.05, 2), "tasks": made}


def email_footer(con):
    return db.kv_get(con, "di_email_footer") or ""


def email_footer_at(con):
    return db.kv_get(con, "di_email_footer_at") or ""


def set_email_footer(con, text, at=None):
    """The footer lives in the app's Settings and here; whichever was changed last wins (the app sends its change time)."""
    t = str(text or "").strip()
    if len(t) < 12:
        raise ValueError("The footer needs your name and a mailing address.")
    when = str(at or "")[:40] or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    db.kv_set(con, "di_email_footer", t[:400]); db.kv_set(con, "di_email_footer_at", when); con.commit()
    return {"footer": t[:400], "at": when}


def campaign_from_search(con, sid, name=None, daily_limit=None, source="owner"):
    """'Draft intro emails for the companies the SDR found': your words accept the search results; every email still waits for your review."""
    s = search_get(con, sid)
    if s.get("lookup_running"):
        raise ValueError(f"The SDR is still looking up emails for {s['area']} ({s['lookups_done']} of {s['lookups']} done). Ask again when it's finished.")
    if s.get("campaign_id"):
        cur = _camp(con, s["campaign_id"])
        if cur["status"] not in ("drafting", "review"):
            raise ValueError("Emails for that search were already approved. Start a new search for more companies.")
    if not s["parts_done"]:
        raise ValueError("The SDR hasn't finished any of that search yet.")
    footer = email_footer(con)
    if not footer:
        raise ValueError("Set your email footer (name and mailing address) in JARVIS Settings first.")
    for tid in s["find_tasks"]:
        t = get_task(con, tid, events=False)
        if t["status"] == "completed" and t.get("approval_state") not in ("accepted", "discarded"):
            agents_ai.review(con, tid, "accept")
    pros = []
    for r in _search_found(con, s):
        em = str(r.get("email") or "").strip().lower()
        if not EMAIL_RE.match(em):
            continue
        info = {k: str(r.get(k) or "")[:300] for k in ("company", "website", "city", "state", "phone", "units", "notes", "source")}
        info.update(email=em, area=s["area"], industry=s.get("industry") or "property management")
        if r.get("email_source"):
            info["source"] = str(r["email_source"])[:300]
        snap = {"company": info["company"], "website": info["website"], "city": info["city"], "state": info["state"], "units": info["units"],
                "industry": (s.get("industry") or "property management").title(), "notes": info["notes"][:500], "source": info["source"], "stage": "Lead"}
        pros.append({"prospect_id": "dpw_" + hashlib.sha1(em.encode()).hexdigest()[:12], "company": info["company"], "to": em, "snapshot": snap, "info": info})
    if not pros:
        raise ValueError("None of the companies found have a public email address, so there's no one to email.")
    if s.get("campaign_id"):  # emails found later by the lookup join the batch you haven't approved yet
        have = {r["prospect_id"] for r in con.execute("SELECT prospect_id FROM hq_campaign_items WHERE campaign_id=?", (s["campaign_id"],))}
        pros = [p for p in pros if p["prospect_id"] not in have]
        if not pros:
            raise ValueError("Emails for that search are already being drafted; there's nothing new to add.")
        r = campaign_add(con, s["campaign_id"], pros)
        return dict(r, id=s["campaign_id"], search=sid, no_email=s["found"] - s["with_email"], added_to_existing=True)
    c = campaign_create(con, name or f"{s['area']} intro emails", daily_limit, footer, pros, s["find_tasks"])
    con.execute("UPDATE hq_prospect_searches SET campaign_id=? WHERE id=?", (c["id"], sid)); con.commit()
    return dict(c, search=sid, no_email=s["found"] - s["with_email"])


def campaign_prospects(con, limit=1000):
    """Prospects that campaigns created on the PC (from a search), so the app can add them to AI Sidekicks' prospect list."""
    out = []
    for r in con.execute("SELECT prospect_id, info, campaign_id FROM hq_campaign_items WHERE info IS NOT NULL ORDER BY id DESC LIMIT ?", (int(limit),)):
        try:
            out.append({"prospect_id": r["prospect_id"], "campaign_id": r["campaign_id"], "info": json.loads(r["info"])})
        except ValueError:
            pass
    return {"prospects": out}


def di_voice_status(con, cfg=None):
    """Everything AI Sidekicks' outreach is doing, for "Hey Jarvis" and the app's assistant."""
    st = state(con, cfg or {})
    ag = {a["name"]: {"state": a["state"], "doing": (a["running"] or {}).get("title"), "queue": a["queue"]}
          for k, a in st["agents"].items() if k in ("sdr", "ae", "architect", "account_mgr")}
    reps = email_replies(con, limit=20)["replies"]
    cs = campaigns(con)["campaigns"][:5]
    return {"agents": ag, "follow_ups_due": len(followups_due(con)["due"]),
            "recent_replies": [{"from": r["from_addr"], "class": r["class"] or "not read yet", "at": r["at"]} for r in reps[:8]],
            "searches": [{"id": x["id"], "area": x["area"], "asked_for": x["count"], "parts_done": f"{x['parts_done']} of {x['parts']}", "found": x["found"],
                          "with_email": x["with_email"], "emails_started": bool(x["campaign_id"]),
                          "email_lookup": (f"{x['lookups_done']} of {x['lookups']} done, {x['emails_from_lookup']} emails found" if x["lookups"] else "not run")} for x in searches(con, 5)["searches"]],
            "campaigns": [{"name": c["name"], "status": c["status"], "companies": c["total"], "sent": c["counts"].get("sent", 0),
                           "ready_for_review": c["counts"].get("ready", 0), "drafting": c["counts"].get("drafting", 0),
                           "need_fixing": c["counts"].get("needs_edit", 0) + c["counts"].get("failed", 0),
                           "results": {k: c["stats"][k] for k in ("sent", "replied", "interested", "wants_call", "booked_calls", "opted_out")}} for c in cs],
            "engine_email": engine_sender(con),
            "approvals_waiting": [t["title"] for t in st["approvals"] if t.get("division") == "demigod"][:8],
            "email_footer_set": bool(email_footer(con)),
            "note": "Reviewing and approving emails happens in the JARVIS app (Prospects → the campaign). Voice can find companies, start drafts and pause sending."}


# ---------------------------------------------------------------- AI Sidekicks signature (one identity on every email)
DI_COMPANY, DI_TAGLINE = agents_ai.BRAND, agents_ai.BRAND_TAGLINE  # AI Sidekicks on every email; AI Sidekicks is the company behind it


def di_signature(con):
    return _sig_from_footer(email_footer(con))


def _sig_from_footer(f):
    if not f:
        return None
    name, addr = f, ""
    for co in (", Demigod Intelligence · ", ", " + DI_COMPANY + " · "):  # the first is the old company name, still read from older footers
        if co in f:
            name, _, addr = f.partition(co)
            break
    if not addr:
        name, _, addr = f.partition(" · ")
    return {"name": name.strip()[:80], "company": DI_COMPANY, "tagline": DI_TAGLINE, "address": addr.strip()[:200]}


def _strip_sig(body):
    """Drop a sign-off the writer added anyway (name, then AI Sidekicks, at the very end): the signature block replaces it."""
    lines = body.rstrip().split("\n")
    names = ("demigod intelligence", DI_COMPANY.lower(), DI_TAGLINE.lower())
    if len(lines) > 3 and lines[-1].strip().lower() in names:
        lines.pop()
        if lines and lines[-1].strip().lower() in names:
            lines.pop()
        if lines and 1 <= len(lines[-1].split()) <= 4 and not lines[-1].rstrip().endswith((".", "?", "!", ":", ",")):
            lines.pop()
        while lines and lines[-1].strip().lower() in ("", "best,", "thanks,", "best regards,", "regards,", "cheers,"):
            lines.pop()
    return "\n".join(lines).rstrip() or body


def sig_text(sig):
    return "\n".join(x for x in (sig["name"], sig["company"], sig["tagline"], sig.get("address")) if x)


def sig_html(sig, logo_cid="aisidekicks-logo"):
    import html as _h
    e = _h.escape
    return ('<table cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;font-family:Arial,Helvetica,sans-serif;color:#1f2a37">'
            f'<tr><td style="font-size:14px;font-weight:bold;line-height:20px">{e(sig["name"])}</td></tr>'
            f'<tr><td style="font-size:13px;line-height:18px">{e(sig["company"])}</td></tr>'
            f'<tr><td style="font-size:12px;line-height:18px;color:#2563EB">{e(sig["tagline"])}</td></tr>'
            + (f'<tr><td style="font-size:11px;line-height:16px;color:#6b7280">{e(sig["address"])}</td></tr>' if sig.get("address") else "")
            + (f'<tr><td style="padding-top:10px"><img src="cid:{logo_cid}" width="200" alt="{e(sig["company"])}" style="display:block;width:200px;height:auto;border:0"></td></tr>' if logo_cid else "")
            + '</table>')


def render_email(params):
    """(text, html) for an approved email: your words, then the signature. The app and the engine send the same thing."""
    import html as _h
    body, sig = params.get("body") or "", params.get("signature")
    text = body + ("\n\n" + sig_text(sig) if sig else "")
    paras = "".join(f'<p style="margin:0 0 12px 0">{_h.escape(p).replace(chr(10), "<br>")}</p>' for p in body.split("\n\n") if p.strip())
    html = f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:21px;color:#1f2a37">{paras}{sig_html(sig) if sig else ""}</div>'
    return text, html


# ---------------------------------------------------------------- campaign scoreboard
def campaign_stats(con, cid):
    items = [dict(r) for r in con.execute("SELECT * FROM hq_campaign_items WHERE campaign_id=?", (cid,))]
    sent = [i for i in items if i["status"] == "sent"]
    tasks = {i["email_task"]: i for i in items if i["email_task"]}
    reps = {}
    if tasks:
        for r in con.execute(f"SELECT * FROM hq_email_replies WHERE email_task IN ({','.join('?' * len(tasks))}) ORDER BY id", list(tasks)):
            reps.setdefault(r["email_task"], r)
    cls = {}
    for r in reps.values():
        k = r["class"] or "unread"
        cls[k] = cls.get(k, 0) + 1
    pids = {i["prospect_id"] for i in sent}
    booked = sum(1 for r in con.execute("SELECT params FROM hq_tasks WHERE kind='ae.book_meeting' AND status='completed'")
                 if json.loads(r["params"] or "{}").get("prospect_id") in pids)
    dnc = email_dnc(con)
    bounced = email_bounced(con)
    n = len(sent)
    nb = sum(1 for i in sent if (i["to_addr"] or "").lower() in bounced)
    return {"sent": n, "replied": len(reps), "reply_rate": round(100 * len(reps) / n, 1) if n else 0, "interested": cls.get("interested", 0),
            "bounced": nb, "bounce_rate": round(100 * nb / n, 1) if n else 0, "enough_to_judge": n >= 100, "benchmark": REPLY_BENCHMARK,
            "wants_call": cls.get("meeting", 0), "questions": cls.get("question", 0), "not_now": cls.get("not_now", 0), "unread": cls.get("unread", 0),
            "opted_out": sum(1 for i in sent if i["to_addr"] in dnc), "booked_calls": booked, "waiting": sum(1 for i in items if i["status"] in ("approved", "sending"))}


# ---------------------------------------------------------------- engine sender: Gmail from the PC, no Claude app needed
def engine_sender(con):
    from . import gmail
    on = (db.kv_get(con, "di_engine_send") or "on") != "off"
    last = db.kv_get(con, "hq_last_tick") or ""
    alive = bool(last) and last >= (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    return {"configured": gmail.configured(), "enabled": on, "loop": alive, "active": gmail.configured() and on and alive,
            "account": db.kv_get(con, "di_gmail_account") or "", "last_error": db.kv_get(con, "di_gmail_error") or "",
            "reply_check": json.loads(db.kv_get(con, "di_reply_check") or "null")}


def set_engine_send(con, on):
    db.kv_set(con, "di_engine_send", "on" if on else "off"); con.commit()
    return engine_sender(con)


def _gmail_send_task(con, t):
    from . import gmail
    text, html = render_email(t["params"])
    return gmail.send(t["params"]["to"], t["params"]["subject"], text, html, t["params"].get("thread_id"))


def engine_send_tick(con, cfg, log=print):
    """Called by the engine loop. Sends at most one due campaign email per call, 2.5-6 minutes apart, under the same rules as the app."""
    import random
    from . import gmail
    st = engine_sender(con)
    if not (gmail.configured() and st["enabled"]):
        return None
    if time.time() < float(db.kv_get(con, "di_engine_next_send") or 0):
        return None
    due = campaign_due(con, cfg)["due"]
    if not due:
        return None
    d = due[0]
    try:
        campaign_claim(con, d["task_id"], cfg)
    except ValueError:
        return None
    t = get_task(con, d["task_id"], events=False)
    try:
        r = _gmail_send_task(con, t)
    except Exception as e:  # noqa: BLE001
        email_failed(con, d["task_id"], str(e))
        db.kv_set(con, "di_gmail_error", str(e)[:300]); con.commit()
        log(f"Campaign email to {d['to']} didn't send: {e}")
        return {"error": str(e)}
    email_sent(con, d["task_id"], r["id"], r["threadId"])
    db.kv_set(con, "di_engine_next_send", str(time.time() + random.uniform(150, 360)))
    db.kv_set(con, "di_gmail_error", ""); con.commit()
    log(f"Campaign email sent to {d['to']} ({d['company']}).")
    return {"sent": d["to"]}


def _quota_error(e):
    t = str(e).lower()
    return "quota" in t or " 429" in t or "rate limit" in t or "ratelimit" in t


def engine_reply_tick(con, cfg, log=print, every_s=600, pause_s=0.4, per_round=40, sleep=time.sleep):
    """Every 10 minutes: read the Gmail threads of emails JARVIS sent and record replies from the person emailed.
    Up to per_round threads each time, rotating through all of them, paced so Gmail's per-user rate limit isn't hit.
    A thread Gmail briefly refuses (rate limit) is retried once after a short wait; the error only shows when most of a round failed."""
    from . import gmail
    if not gmail.configured() or time.time() < float(db.kv_get(con, "di_engine_next_reply") or 0):
        return None
    db.kv_set(con, "di_engine_next_reply", str(time.time() + every_s)); con.commit()
    sent = email_history(con, None, 500)["sent"]
    ours = {e["message_id"] for e in sent if e["message_id"]}
    threads = {}
    for e in sent:
        if e["thread_id"]:
            threads.setdefault(e["thread_id"], []).append(e)
    tids = sorted(threads)
    start = int(db.kv_get(con, "di_reply_cursor") or 0) % max(1, len(tids))
    batch = (tids[start:] + tids[:start])[:per_round]
    db.kv_set(con, "di_reply_cursor", str(start + len(batch)))
    found, checked, failed, streak, last_err = 0, 0, 0, 0, ""
    for tid in batch:
        es = threads[tid]
        if checked:
            sleep(pause_s)
        checked += 1
        msgs = None
        for attempt in range(2):
            try:
                msgs = gmail.thread(tid)
                break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:300]
                if attempt == 0 and _quota_error(e):
                    sleep(3)  # Gmail asked us to slow down: wait a moment and try this thread once more
                    continue
                break
        if msgs is None:
            failed += 1
            streak += 1
            if streak >= 3 and _quota_error(last_err):
                break  # Gmail keeps refusing: stop this round; the next one picks up from here
            continue
        streak = 0
        for m in msgs:
            if not m["id"] or m["id"] in ours:
                continue
            frm = (m["from"].split("<")[-1].rstrip(">") if "<" in m["from"] else m["from"]).strip().lower()
            if BOUNCE_FROM.search(m["from"] or "") and BOUNCE_TEXT.search(m.get("text") or ""):
                at = datetime.fromtimestamp(int(m["internalDate"]) / 1000).isoformat(timespec="seconds") if m.get("internalDate") else now()
                txt = (m.get("text") or "").lower()
                hit = next((e for e in es if (e["to"] or "").lower() and (e["to"] or "").lower() in txt), es[-1])
                if email_bounce_add(con, hit["to"], m["id"], hit["id"], (BOUNCE_TEXT.search(m.get("text") or "").group(0)), at)["new"]:
                    found += 1
                continue
            mine = [e for e in es if (e["to"] or "").lower() == frm]
            if not mine:
                continue
            last = mine[-1]
            at = datetime.fromtimestamp(int(m["internalDate"]) / 1000).isoformat(timespec="seconds") if m.get("internalDate") else now()
            if last["sent_at"] and at < last["sent_at"][:19]:
                continue
            try:
                if email_reply_add(con, last["id"], m["id"], frm, at, m["text"])["new"]:
                    found += 1
            except ValueError:
                pass
    if found:
        log(f"{found} new repl{'y' if found == 1 else 'ies'} to AI Sidekicks outreach.")
    db.kv_set(con, "di_reply_check", json.dumps({"at": now(), "checked": checked, "failed": failed, "threads": len(tids), "found": found}))
    if checked and failed * 2 >= checked:
        db.kv_set(con, "di_gmail_error", last_err)  # most of the round failed: that's worth showing
    elif checked:
        db.kv_set(con, "di_gmail_error", "")
    con.commit()
    return {"found": found, "checked": checked, "failed": failed}


def engine_test_send(con, cid, cfg=None):
    """Send one email from a campaign to your own Gmail address (the account JARVIS is signed in as), marked TEST. Counts for nothing."""
    from . import gmail
    if not gmail.configured():
        raise ValueError("Engine email isn't set up yet (run gmail-auth on the PC).")
    me = (gmail.profile().get("emailAddress") or "").lower()
    if not EMAIL_RE.match(me):
        raise ValueError("Couldn't read your Gmail address.")
    g = campaign_get(con, cid, cfg)
    it = next((i for i in g["items"] if i["status"] in ("ready", "approved", "sent") and i["body"]), None)
    if not it:
        raise ValueError("No drafted email in that campaign yet.")
    p = {"to": me, "subject": "TEST: " + it["subject"], "body": _strip_sig(it["body"]), "signature": di_signature(con)}
    text, html = render_email(p)
    r = gmail.send(me, p["subject"], text, html)
    return {"to": me, "subject": p["subject"], "company": it["company"], "message_id": r["id"]}

# ---------------------------------------------------------------- Morning brief (both businesses, one read)
def morning_brief(con, cfg=None, hours=24):
    """What needs Mola this morning, straight from the engine's own records. Read-only: it changes nothing."""
    cfg = cfg or {}
    t = crm.today()
    since_ = (datetime.now() - timedelta(hours=int(hours))).isoformat(timespec="seconds")
    tomorrow = (date.fromisoformat(t) + timedelta(days=1)).isoformat()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731

    # MolaBucks: follow-ups, tasks, new county leads, seller texts
    allq = crm.queue(con)
    q = [d for d in allq if d.get("nextFollowUp")]  # follow-ups you set; brand-new leads never contacted are counted separately
    fu = [{"address": d.get("address"), "seller": d.get("seller"), "stage": d.get("stage"), "due": d.get("nextFollowUp"),
           "overdue": d["nextFollowUp"] < t} for d in q[:6]]
    tasks = [x for x in crm.items(con, "tasks") if not x.get("done") and x.get("due") and x["due"] <= t]
    ov = crm.overview(con)
    try:
        leads = [dict(r) for r in con.execute("SELECT county,state,source_type,address,event_date FROM leads WHERE first_seen>=? ORDER BY first_seen DESC LIMIT 5", (since_,))]
        new_leads = one("SELECT COUNT(*) FROM leads WHERE first_seen>=?", since_)
        waiting_leads = one("SELECT COUNT(*) FROM leads WHERE status='new' AND " + db.UPCOMING)
    except Exception:
        leads, new_leads, waiting_leads = [], 0, 0
    try:  # motivated-seller records from the county clerk (pre-foreclosure, probate, evictions), new since yesterday
        clerk = {r[0]: r[1] for r in con.execute("SELECT category, COUNT(*) FROM lead_events WHERE source_id LIKE 'fl-pinellas-clerk-%' AND retrieved_at>=? GROUP BY category", (since_,))}
    except Exception:
        clerk = {}
    try:
        texts = [dict(r) for r in con.execute("SELECT phone, body, at FROM messages WHERE direction='in' AND at>=? ORDER BY at DESC LIMIT 5", (since_,))]
        text_count = one("SELECT COUNT(*) FROM messages WHERE direction='in' AND at>=?", since_)
    except Exception:
        texts, text_count = [], 0
    molabucks = {"follow_ups_due": len(q), "follow_ups": fu, "new_leads_not_contacted": len(allq) - len(q), "tasks_due": [{"title": x.get("title"), "due": x.get("due"), "overdue": x["due"] < t} for x in tasks[:6]],
                 "tasks_due_count": len(tasks), "new_county_leads": new_leads, "county_leads_sample": leads, "county_leads_waiting": waiting_leads,
                 "new_clerk_records": clerk,
                 "seller_texts": text_count, "seller_texts_sample": [{"phone": m["phone"], "text": (m["body"] or "")[:160], "at": m["at"]} for m in texts],
                 "goal": ov["goal"], "collected_this_month": ov["collectedThisMonth"], "under_contract": ov["countsByStage"].get("Under Contract", 0),
                 "closing_scheduled": ov["countsByStage"].get("Closing Scheduled", 0)}

    # AI Sidekicks: replies, calls, campaign scoreboard, what goes out today
    reps = [dict(r) for r in con.execute("SELECT prospect_id, from_addr, at, class, text FROM hq_email_replies WHERE at>=? ORDER BY at DESC LIMIT 10", (since_,))]
    calls = []
    for r in con.execute("SELECT id, status, params FROM hq_tasks WHERE kind='ae.book_meeting' AND status IN ('completed','awaiting_approval','queued','waiting')"):
        pr = json.loads(r["params"] or "{}")
        st = str(pr.get("start") or "")
        if st[:10] in (t, tomorrow):
            try:
                br = con.execute("SELECT status FROM hq_precall WHERE (prospect_id=? AND prospect_id!='') OR email=? ORDER BY created DESC LIMIT 1",
                                 (pr.get("prospect_id") or "", pr.get("attendee") or "")).fetchone()
            except Exception:
                br = None
            calls.append({"title": pr.get("title"), "with": pr.get("attendee"), "start": st, "day": "today" if st[:10] == t else "tomorrow",
                          "booked": r["status"] == "completed", "brief_ready": bool(br and br["status"] == "ready")})
    calls.sort(key=lambda c: c["start"])
    cs = campaigns(con)["campaigns"]
    live = [c for c in cs if c["status"] in ("sending", "approved")]
    queued = sum(c["counts"].get("approved", 0) for c in live)
    limit = sum(int(c.get("daily_limit") or 0) for c in live)
    demigod = {"replies": len(reps), "replies_sample": [{"from": r["from_addr"], "class": r["class"] or "not read yet", "at": r["at"], "text": (r["text"] or "")[:200]} for r in reps[:5]],
               "wants_call": sum(1 for r in reps if r["class"] in YES), "unread": sum(1 for r in reps if not r["class"]),
               "calls": calls, "follow_ups_due": len(followups_due(con)["due"]),
               "sending_today": min(queued, limit) if limit else queued, "campaigns": [{"name": c["name"], "status": c["status"], "sent": c["stats"]["sent"], "replied": c["stats"]["replied"],
               "reply_rate": c["stats"]["reply_rate"], "booked_calls": c["stats"]["booked_calls"], "ready_for_review": c["counts"].get("ready", 0)} for c in cs[:3]]}

    appr = [dict(r) for r in con.execute("SELECT id, agent, title, division, created FROM hq_tasks WHERE status='awaiting_approval' ORDER BY created LIMIT 10")]
    probs = [dict(r) for r in con.execute("SELECT agent, title, status, error FROM hq_tasks WHERE status IN ('failed','blocked') AND updated>=? ORDER BY updated DESC LIMIT 5", (since_,))]
    try:
        sysck = json.loads(db.kv_get(con, "system_check_last") or "null")
    except ValueError:
        sysck = None
    tick = db.kv_get(con, "hq_last_tick") or ""
    try:
        loop_ok = bool(tick) and (datetime.now() - datetime.fromisoformat(tick[:19])).total_seconds() < 600
    except ValueError:
        loop_ok = False
    try:
        spent = agents_ai.usage(con)
    except Exception:
        spent = None
    es = engine_sender(con)
    if "sign-in expired" in str(es.get("last_error") or ""):
        probs.insert(0, {"agent": "sdr", "title": "Engine email paused: Gmail sign-in expired", "status": "blocked", "error": es["last_error"]})
    out = {"date": t, "since": since_, "molabucks": molabucks, "demigod": demigod,
           "approvals_waiting": len(appr), "approvals": [{"title": a["title"], "agent": a["agent"], "division": a.get("division"), "since": a["created"]} for a in appr],
           "problems": [{"agent": p["agent"], "title": p["title"], "status": p["status"], "error": (p["error"] or "")[:160]} for p in probs],
           "engine": {"loop_running": loop_ok, "last_tick": tick, "email_sender": engine_sender(con)}, "ai_spend": spent,
           "system_check": sysck, "demigod_crm": _dcrm_brief(con)}
    out["headline"] = _brief_line(out)
    return out


def _dcrm_brief(con):
    try:
        from . import dcrm
        s = dcrm.summary(con)
        new = con.execute("SELECT COUNT(*) FROM dc_businesses WHERE discovered_at>=?", ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()[0]
        return {"total": s["total"], "new_24h": new, "high_priority": s["by_priority"].get("High", 0),
                "research_required": s["by_priority"].get("Research required", 0),
                "open_by_stage": {k: v for k, v in s["by_stage"].items() if v and k not in ("Lost", "Disqualified")}}
    except Exception:
        return None


def _clock(iso):
    try:
        h, mi = int(iso[11:13]), int(iso[14:16])
    except (ValueError, IndexError):
        return iso[11:16]
    return f"{h % 12 or 12}{':%02d' % mi if mi else ''} {'AM' if h < 12 else 'PM'}"


def _brief_line(b):
    m, d, bits = b["molabucks"], b["demigod"], []
    def n(k, one, many=None):
        return f"{k} {one if k == 1 else (many or one + 's')}"
    c = next((c for c in d["calls"] if c["day"] == "today"), None)
    if c:
        bits.append(f"an AI Sidekicks call at {_clock(c['start'])}")
    if d["replies"]:
        bits.append(n(d["replies"], "AI Sidekicks reply", "AI Sidekicks replies") + (f" ({d['wants_call']} {'wants' if d['wants_call'] == 1 else 'want'} a call)" if d["wants_call"] else ""))
    if d.get("follow_ups_due"):
        bits.append(n(d["follow_ups_due"], "AI Sidekicks follow-up") + " due")
    if m["follow_ups_due"]:
        bits.append(n(m["follow_ups_due"], "seller follow-up"))
    if m["tasks_due_count"]:
        bits.append(n(m["tasks_due_count"], "task") + " due")
    if m["seller_texts"]:
        bits.append(n(m["seller_texts"], "seller text"))
    if m["new_county_leads"]:
        bits.append(n(m["new_county_leads"], "new county lead"))
    if b["approvals_waiting"]:
        bits.append(n(b["approvals_waiting"], "approval") + " waiting")
    if not bits:
        return "Nothing needs you this morning."
    return "Today: " + (", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]) + "."


READ_OPS = {"state", "tasks", "task", "reports", "chats", "plan_preview", "since", "health", "usage", "integrity", "email_outbox",
            "email_history", "email_replies", "followups_due", "action_outbox", "prospect_status", "campaigns", "campaign_get", "campaign_due", "searches", "search_get", "campaign_prospects", "di_status", "email_footer", "di_offer", "engine_sender", "campaign_stats", "morning_brief", "precall_list", "precall_get", "duties", "exec_brief", "di_crm_summary", "di_businesses", "di_business", "di_pricing", "demo_pages", "demo_page", "di_duplicates", "li_today", "telegram_status", "team", "voice_calls_status"}
WRITE_OPS = {"create_task", "cancel", "retry", "reprioritize", "approve", "reject", "set_goal", "chat_log", "run_now", "pause", "resume",
             "set_ai_cap", "review", "delegate", "email_sent", "email_failed", "email_dnc_add",
             "email_reply_add", "action_done", "action_failed", "campaign_create", "campaign_add", "campaign_edit_item", "campaign_approve", "campaign_set", "campaign_claim", "campaign_redraft", "set_di_offer", "set_engine_send", "engine_test_send", "search_start", "campaign_from_search", "set_email_footer", "search_enrich", "precall_build", "set_ai_budgets", "di_business_upsert", "di_move_stage", "di_add_contact", "di_import_app", "di_crm_sync", "di_research", "email_bounce_add", "di_proposal", "set_di_pricing", "demo_page_build", "di_merge", "li_build", "li_mark", "li_add", "employee_talk", "telegram_unpair", "voice_calls_setup", "voice_calls_install_tunnel"}


def integrity(con):
    """Read-only checks over the engine's own records. Reports problems; never repairs anything by itself."""
    checks = []
    def chk(name, bad, detail_ok, detail_bad):
        checks.append({"name": name, "ok": not bad, "detail": detail_ok if not bad else detail_bad})
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    chk("Database file", ic != "ok", "SQLite integrity check passed", f"SQLite reports: {ic}")
    at = now()
    stale = con.execute("SELECT id,title FROM hq_tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?", (at,)).fetchall()
    chk("Stuck tasks", stale, "No running task has an expired lease", f"{len(stale)} running task(s) with expired leases (recovered on the next worker tick): " + ", ".join(r["title"] for r in stale[:5]))
    ungated = con.execute("SELECT id,title FROM hq_tasks WHERE level>=3 AND status IN ('running','completed') AND (approved_hash IS NULL OR approved_hash<>params_hash)").fetchall()
    chk("Approval gate", ungated, "Every approval-level task that ran was approved for its exact parameters", f"{len(ungated)} approval-level task(s) ran without a matching approval: " + ", ".join(r["title"] for r in ungated[:5]))
    unverified = con.execute("SELECT COUNT(*) FROM hq_tasks WHERE status='completed' AND (verified IS NULL OR verified=0)").fetchone()[0]
    chk("Verified completions", unverified, "Every completed task was verified", f"{unverified} task(s) marked completed without verification")
    ids = {r[0] for r in con.execute("SELECT id FROM hq_tasks")}
    broken = [r["id"] for r in con.execute("SELECT id, depends_on FROM hq_tasks WHERE depends_on IS NOT NULL AND depends_on<>'[]'") if any(d not in ids for d in (json.loads(r["depends_on"] or "[]") or []))]
    chk("Task dependencies", broken, "All dependencies point to real tasks", f"{len(broken)} task(s) depend on a task that doesn't exist")
    cross = []
    for r in con.execute("SELECT id, agent, division FROM hq_tasks"):
        a = AGENTS.get(r["agent"])
        if a and a["division"] not in ("exec", r["division"]):
            cross.append(r["id"])
    chk("Business separation", cross, "No agent holds work for the other business", f"{len(cross)} task(s) assigned across businesses")
    unknown = [r[0] for r in con.execute("SELECT DISTINCT kind FROM hq_tasks") if r[0] not in KINDS]
    chk("Task types", unknown, "Every task type has an executor", "Unknown task types in the queue: " + ", ".join(unknown[:5]))
    day = datetime.now().strftime("%Y-%m-%d")
    over = agents_ai.spent(con, day) > agents_ai.cap(con) + 0.01
    chk("AI spend cap", over, f"Today's AI spend ${agents_ai.spent(con, day):.2f} is within the ${agents_ai.cap(con):.2f} cap", f"Today's AI spend ${agents_ai.spent(con, day):.2f} is over the ${agents_ai.cap(con):.2f} cap")
    lt = db.kv_get(con, "hq_last_tick")
    try:
        age = (datetime.now() - datetime.fromisoformat(lt)).total_seconds() if lt else None
    except ValueError:
        age = None
    chk("Worker loop", age is None or age > 180, f"Last worker tick {int(age)}s ago" if age is not None else "", "The worker loop hasn't ticked in the last 3 minutes. Run Start-JARVIS on the PC." if lt else "The worker loop has never run on this database.")
    errs = con.execute("SELECT COUNT(*) FROM hq_events WHERE type IN ('failed','blocked') AND at>=?", ((datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchone()[0]
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "failures_24h": errs, "at": at}


def delegate(con, request, business, context=None):
    """JARVIS / Chief of Staff routing by department. Never routes to an approval-gated (level 3) task."""
    request = str(request or "").strip()
    if not request:
        raise ValueError("Say what you need done.")
    if business not in org.BUSINESSES:
        raise ValueError("Pick the business this is for.")
    kind = agents_ai.route(request, business)
    if not kind:
        raise ValueError("No agent for that business handles this kind of request yet. Assign it from an agent's panel.")
    params = dict(context or {})
    params.setdefault("request", request[:1000])
    for k in ("topic", "subject"):
        params.setdefault(k, request[:300])
    t = create_task(con, kind, params, title=request[:120], source="jarvis", division=business)
    event(con, t["id"], "jarvis", "delegated", f"JARVIS routed this to {AGENTS[t['agent']]['name']} ({AGENTS[t['agent']]['department']})")
    con.commit()
    return {"task": t, "agent": t["agent"], "kind": kind}


def handle(con, cfg, req):
    a = req or {}
    op = a.get("op")
    if op == "state":
        return state(con, cfg)
    if op == "tasks":
        return {"tasks": list_tasks(con, a.get("agent"), a.get("statuses"), a.get("limit") or 100, a.get("goal_id"), a.get("division"))}
    if op == "task":
        return get_task(con, a.get("id"))
    if op == "reports":
        return {"reports": reports(con, a.get("limit") or 20)}
    if op == "chats":
        return {"chats": chats(con, a.get("agent"))}
    if op == "since":
        return since(con, a.get("ts"))
    if op == "health":
        return health(con)
    if op == "usage":
        return agents_ai.usage(con)
    if op == "integrity":
        return integrity(con)
    if op == "email_outbox":
        return email_outbox(con)
    if op == "email_sent":
        return email_sent(con, a.get("id"), a.get("message_id"), a.get("thread_id"))
    if op == "email_failed":
        return email_failed(con, a.get("id"), a.get("error"))
    if op == "email_bounce_add":
        return email_bounce_add(con, a.get("email"), reason=a.get("reason") or "recorded by the owner")
    if op == "email_dnc_add":
        return email_dnc_add(con, a.get("address"))
    if op == "email_history":
        return email_history(con, a.get("prospect_id"), a.get("days") or 60)
    if op == "email_reply_add":
        return email_reply_add(con, a.get("email_task"), a.get("message_id"), a.get("from"), a.get("at"), a.get("text"))
    if op == "email_replies":
        return email_replies(con, a.get("prospect_id"))
    if op == "followups_due":
        return followups_due(con)
    if op == "action_outbox":
        return action_outbox(con, a.get("kinds"))
    if op == "action_done":
        return action_done(con, a.get("id"), a.get("result"))
    if op == "action_failed":
        return action_failed(con, a.get("id"), a.get("error"))
    if op == "prospect_status":
        return prospect_status(con, a.get("prospect_id"))
    if op == "campaigns":
        return campaigns(con)
    if op == "campaign_get":
        return campaign_get(con, a.get("id"), cfg)
    if op == "campaign_due":
        return campaign_due(con, cfg)
    if op == "campaign_create":
        return campaign_create(con, a.get("name"), a.get("daily_limit"), a.get("footer"), a.get("prospects"), a.get("find_tasks"))
    if op == "campaign_add":
        return campaign_add(con, a.get("id"), a.get("prospects"))
    if op == "campaign_edit_item":
        return campaign_edit_item(con, a.get("id"), a.get("item_id"), a.get("subject"), a.get("body"), bool(a.get("drop")))
    if op == "campaign_approve":
        return campaign_approve(con, a.get("id"), a.get("items_hash"), cfg)
    if op == "campaign_set":
        return campaign_set(con, a.get("id"), a.get("action"), a.get("daily_limit"))
    if op == "campaign_claim":
        return campaign_claim(con, a.get("task_id"), cfg)
    if op == "searches":
        return searches(con)
    if op == "search_get":
        return search_get(con, a.get("id"))
    if op == "search_start":
        return search_start(con, a.get("area"), a.get("count"), "app")
    if op == "campaign_from_search":
        return campaign_from_search(con, a.get("id"), a.get("name"), a.get("daily_limit") or GMAIL_DAILY_MAX, "app")
    if op == "campaign_redraft":
        return campaign_redraft(con, a.get("id"), bool(a.get("all")))
    if op == "engine_sender":
        return engine_sender(con)
    if op == "set_engine_send":
        return set_engine_send(con, bool(a.get("on")))
    if op == "engine_test_send":
        return engine_test_send(con, a.get("id"), cfg)
    if op == "campaign_stats":
        return campaign_stats(con, a.get("id"))
    if op == "di_proposal":
        return di_proposal(con, a.get("company"), a.get("notes"), a.get("dc_business_id") or a.get("id"), a.get("prospect_id"))
    if op == "di_pricing":
        return di_pricing(con)
    if op == "set_di_pricing":
        return set_di_pricing(con, a.get("text"))
    if op in ("demo_pages", "demo_page", "demo_page_build"):
        from . import demo_page
        if op == "demo_pages":
            return demo_page.list_pages(con)
        if op == "demo_page":
            return demo_page.get(con, a.get("key") or a.get("company") or "")
        return demo_page.build(con, prospect_id=a.get("prospect_id"), business_id=a.get("dc_business_id") or a.get("id"),
                               company=a.get("company"), meeting=a.get("meeting"), force=bool(a.get("force")))
    if op in ("voice_calls_setup", "voice_calls_install_tunnel", "voice_calls_status"):
        from . import voice_bridge  # live calls: owner-triggered setup (the same as 'python run.py voice-calls')
        if op == "voice_calls_status":
            st = voice_bridge.state()
            return {"agents": sorted(st.get("agents") or {}), "tunnel_installed": bool(voice_bridge.find_cloudflared()), "public": bool(st.get("public_url")),
                    "tunnel": voice_bridge.tunnel_status()}
        if op == "voice_calls_install_tunnel":
            return voice_bridge.install_cloudflared()
        v = (cfg or {}).get("voice") or {}
        voices = dict(v.get("agent_voices") or {})
        if v.get("elevenlabs_voice_id"):
            voices.setdefault("jarvis", str(v["elevenlabs_voice_id"]))
        return {"agents": sorted(voice_bridge.setup(voices=voices, log=lambda *x: None))}
    if op in ("employee_talk", "team", "telegram_status", "telegram_unpair"):
        from . import employees, telegram  # talk to any employee (app, voice, phone); the phone line's pairing
        if op == "team":
            return {"team": employees.team()}
        if op == "telegram_status":
            return telegram.status(con)
        if op == "telegram_unpair":
            return telegram.unpair(con)
        return employees.talk(con, cfg, a.get("agent") or "jarvis", a.get("text") or a.get("request"), source=str(a.get("source") or "owner_app")[:20])
    if op in ("li_today", "li_build", "li_mark", "li_add"):
        from . import linkedin  # LinkedIn owner list: drafts only, the owner sends every message himself
        if op == "li_today":
            return linkedin.today(con)
        if op == "li_build":
            return dict(linkedin.build(con, a.get("limit") or linkedin.DAILY), list=linkedin.today(con))
        if op == "li_mark":
            return linkedin.mark(con, a.get("id") or a.get("business_id"), a.get("status"), profile_url=a.get("profile_url"),
                                 note=a.get("note"), followup=a.get("followup"), reply=a.get("reply"))
        return linkedin.add(con, a.get("company"), a.get("owner") or "", a.get("title") or "", a.get("profile_url") or "",
                            a.get("niche") or "", a.get("city") or "", a.get("state") or "MA")
    if op in ("di_merge", "di_duplicates"):
        from . import dcrm
        dc = dcrm.connect()
        if op == "di_duplicates":
            return dcrm.duplicates(dc)
        return dcrm.merge(dc, a.get("keep"), a.get("duplicate"), by="owner", why=str(a.get("why") or "merged by the owner")[:200])
    if op == "di_research":
        return di_research(con, req.get("ids"), req.get("priority", "Research required"), req.get("limit") or 25)
    if op.startswith("di_") and op in ("di_crm_summary", "di_businesses", "di_business", "di_business_upsert", "di_move_stage", "di_add_contact", "di_import_app", "di_crm_sync"):
        from . import dcrm
        dc = dcrm.connect()
        if op == "di_crm_summary":
            return dcrm.summary(dc)
        if op == "di_businesses":
            return dcrm.businesses(dc, req.get("filter") or {})
        if op == "di_business":
            return dcrm.detail(dc, req.get("id"))
        if op == "di_business_upsert":
            bid, created, how = dcrm.upsert(dc, req.get("record") or {}, source="owner")
            return {"id": bid, "created": created, "matched_on": how, "business": dcrm.detail(dc, bid)}
        if op == "di_move_stage":
            return dcrm.move(dc, req.get("id"), req.get("stage"), by="owner", note=str(req.get("note") or ""), reason=str(req.get("reason") or ""))
        if op == "di_add_contact":
            c = req.get("contact") or {}
            cid = dcrm.add_contact(dc, req.get("id"), c.get("email", ""), c.get("name", ""), c.get("title", ""), c.get("role", ""), c.get("phone", ""), c.get("source_url", ""))
            dc.commit(); dcrm.score(dc, req.get("id"))
            return {"contact_id": cid, "business": dcrm.detail(dc, req.get("id"))}
        if op == "di_import_app":
            return dcrm.ingest_app(dc, req.get("items") or [])
        return dcrm.sync(dc, do_enrich=False)  # website research runs in the engine's background thread
    if op == "duties":
        return {"duties": duties.board(con), "monitoring": duties.MONITORING, "budgets": duties.budgets(con),
                "spent_today": {a: round(duties.spent_by(con, a), 4) for a in duties.budgets(con)}, "prospecting": duties.conf(cfg),
                "backup": json.loads(db.kv_get(con, "backup_last") or "null"), "system_check": json.loads(db.kv_get(con, "system_check_last") or "null"),
                "engine_online": engine_online(con)}
    if op == "set_ai_budgets":
        return {"budgets": duties.set_budgets(con, req.get("budgets") or {})}
    if op in ("precall_list", "precall_get", "precall_build"):
        from . import precall
        if op == "precall_list":
            return precall.list_briefs(con)
        if op == "precall_get":
            return precall.get(con, req.get("who") or "") or {"status": "none", "summary": "No brief saved for that name yet."}
        return precall.build(con, who=req.get("who"), website=req.get("website"))
    if op == "morning_brief":
        return morning_brief(con, cfg, a.get("hours") or 24)
    if op == "exec_brief":  # executive morning brief: both businesses, workforce, infrastructure, history (read-only)
        from . import exec_brief
        return exec_brief.exec_brief(con, cfg)
    if op == "di_offer":
        return {"offer": di_offer(con), "default": not (db.kv_get(con, "di_offer") or "").strip()}
    if op == "set_di_offer":
        return set_di_offer(con, a.get("offer"))
    if op == "search_enrich":
        return search_enrich(con, a.get("id"), "app")
    if op == "campaign_prospects":
        return campaign_prospects(con)
    if op == "di_status":
        return di_voice_status(con, cfg)
    if op == "email_footer":
        return {"footer": email_footer(con), "at": email_footer_at(con)}
    if op == "set_email_footer":
        return set_email_footer(con, a.get("footer"), a.get("at"))
    if op == "set_ai_cap":
        return agents_ai.set_cap(con, a.get("cap"))
    if op == "review":
        return agents_ai.review(con, a.get("id"), a.get("decision"))
    if op == "delegate":
        return delegate(con, a.get("request"), a.get("division") or a.get("business"), a.get("context"))
    if op == "pause":
        return set_paused(con, True, reason=a.get("reason", ""))
    if op == "resume":
        return set_paused(con, False)
    if op == "plan_preview":
        return build_plan(con, a.get("target") or crm.settings(con)["goal"])
    if op == "create_task":
        kind = a.get("kind")
        if kind in ("jarvis.plan", "jarvis.briefing_report"):
            raise ValueError("JARVIS creates that one itself.")
        return create_task(con, kind, a.get("params") or {}, title=a.get("title"), objective=a.get("objective", ""),
                           priority=a.get("priority") or 3, deadline=a.get("deadline"), source="owner", division=a.get("division"),
                           dedupe_key=a.get("dedupe_key") if str(a.get("dedupe_key") or "").startswith(kind + ":") else None)
    if op == "cancel":
        return cancel(con, a.get("id"))
    if op == "retry":
        return retry(con, a.get("id"))
    if op == "reprioritize":
        return reprioritize(con, a.get("id"), a.get("priority"))
    if op == "approve":
        return approve(con, a.get("id"), a.get("params_hash"))
    if op == "reject":
        return reject(con, a.get("id"), a.get("reason", ""))
    if op == "set_goal":
        return set_goal(con, a.get("target"), a.get("text", ""))
    if op == "chat_log":
        return chat_log(con, a.get("agent"), a.get("role", "owner"), a.get("text", ""), a.get("task_id"))
    if op == "run_now":  # owner asks the worker to run due tasks right away (the PC loop also does this every 30s)
        if paused(con):
            raise ValueError(paused(con) + ". Resume it first.")
        last = db.kv_get(con, "hq_last_tick") or ""
        if last and last >= (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds"):
            # the engine loop is running: hand it over instead of running AI work inside the PC-link server, which would
            # block every other call from the app (that's what made the PC link look like it dropped)
            db.kv_set(con, "hq_run_now", now()); con.commit()
            return {"ran": [], "handed_to_engine": True, "note": "JARVIS Engine starts it within a few seconds."}
        return {"ran": run_due(con, cfg, log=lambda *x: None)}
    raise ValueError(f"Unknown HQ operation: {op}")
