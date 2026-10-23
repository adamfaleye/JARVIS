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
import os
import socket
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import crm, db

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

AGENTS = {
    "jarvis": {"name": "JARVIS", "title": "Chief Executive Officer", "color": "#4fd8ff",
               "mission": "Turns your goal into a plan, assigns the work, checks it, and reports honestly."},
    "hunter": {"name": "Hunter", "title": "Acquisitions Intelligence", "color": "#f3b54a",
               "mission": "Finds, scores and organizes motivated-seller leads from public records."},
    "closer": {"name": "Closer", "title": "Dispositions & Sales", "color": "#46dc9c",
               "mission": "Moves contracted deals to buyers: deal status, buyer demand, closing dependencies."},
    "followup": {"name": "Follow-up", "title": "Pipeline Persistence", "color": "#a58bff",
                 "mission": "Makes sure no lead or buyer goes cold: due follow-ups, drips, reply health."},
    "frontdesk": {"name": "Front Desk", "title": "Communications Intake", "color": "#ff8fb1",
                  "mission": "Sorts incoming texts and calls, drafts replies, escalates what matters."},
    "dispo": {"name": "Dispositions", "title": "Buyer Matching Intelligence", "color": "#3ee0c5",
              "mission": "Matches deals to buyers on real criteria and flags weak demand."},
    "finance": {"name": "Finance", "title": "Finance & Operations", "color": "#e8c46a",
                "mission": "Fees vs. goal, deadlines (EMD, closing), costs and service balances."},
}

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
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def _j(v):
    return json.dumps(v, default=str)


def _h(params):
    return hashlib.sha256(json.dumps(params or {}, sort_keys=True, default=str).encode()).hexdigest()[:24]


def connect():
    con = db.connect()
    con.executescript(SCHEMA)
    return con


def event(con, task_id, agent, type_, message, data=None):
    con.execute("INSERT INTO hq_events(task_id,agent,at,type,message,data) VALUES(?,?,?,?,?,?)",
                (task_id, agent, now(), type_, str(message)[:500], _j(data) if data is not None else None))


# ---------------------------------------------------------------- task lifecycle
def create_task(con, kind, params=None, *, title=None, objective="", priority=3, deadline=None, depends_on=None,
                goal_id=None, parent_id=None, source="owner", dedupe_key=None, test=False, run_at=None):
    if kind not in KINDS:
        raise ValueError(f"Unknown task type: {kind}")
    agent, level, default_title, _ = KINDS[kind]
    params = params or {}
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
                   parent_id,source,dedupe_key,next_run_at,created,updated,level,approval_state,test)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, agent, kind, (title or default_title)[:160], str(objective)[:600], _j(params), _h(params),
                 max(1, min(int(priority or 3), 5)), status, deadline, _j(deps), goal_id, parent_id, source, dedupe_key,
                 run_at or now(), now(), now(), level, "pending" if level >= 3 else "none", 1 if test else 0))
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


def list_tasks(con, agent=None, statuses=None, limit=100, goal_id=None):
    q, a = "SELECT * FROM hq_tasks WHERE 1=1", []
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
        if running:
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
        out[aid] = dict(a, id=aid, state=state, queue=cnt.get("queued", 0) + cnt.get("waiting", 0),
                        approvals=cnt.get("awaiting_approval", 0), running=dict(running) if running else None,
                        last=dict(last) if last else None, last_event=dict(ev) if ev else None,
                        scorecard={"completed_7d": done, "failed_7d": failed, "blocked_7d": sc["blocked"] or 0,
                                   "success_rate": round(100 * done / (done + failed), 0) if done + failed else None,
                                   "avg_seconds": round(sc["secs"] or 0, 1)})
    return out


def state(con, cfg):
    g = active_goal(con)
    rep = con.execute("SELECT * FROM hq_reports ORDER BY at DESC LIMIT 1").fetchone()
    return {"agents": agent_status(con), "goal": g, "plan_preview": None if g else build_plan(con, crm.settings(con)["goal"]),
            "approvals": list_tasks(con, statuses=["awaiting_approval"], limit=50),
            "events": [dict(e) for e in con.execute("SELECT task_id,agent,at,type,message FROM hq_events ORDER BY id DESC LIMIT 40")],
            "counts": {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM hq_tasks GROUP BY status")},
            "deadlines": deadlines(con, 14), "latest_report": (dict(rep, body=json.loads(rep["body"])) if rep else None),
            "kinds": {k: {"agent": v[0], "level": v[1], "title": v[2]} for k, v in KINDS.items()}, "now": now(),
            "worker_loop": db.kv_get(con, "hq_last_tick"), "hunter_leads": _hunter_dots(con), "health": health(con)}


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


READ_OPS = {"state", "tasks", "task", "reports", "chats", "plan_preview", "since", "health"}
WRITE_OPS = {"create_task", "cancel", "retry", "reprioritize", "approve", "reject", "set_goal", "chat_log", "run_now", "pause", "resume"}


def handle(con, cfg, req):
    a = req or {}
    op = a.get("op")
    if op == "state":
        return state(con, cfg)
    if op == "tasks":
        return {"tasks": list_tasks(con, a.get("agent"), a.get("statuses"), a.get("limit") or 100, a.get("goal_id"))}
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
                           priority=a.get("priority") or 3, deadline=a.get("deadline"), source="owner",
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
        return {"ran": run_due(con, cfg, log=lambda *x: None)}
    raise ValueError(f"Unknown HQ operation: {op}")
