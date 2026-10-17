"""Local mirror of the JARVIS CRM (data model v2). The JARVIS app syncs with it (crm_pull / crm_push over MCP),
and the desktop voice assistant reads and writes it through the validated operations below.
Rules match the app: same stages, follow-up rules, one follow-up task per deal, archive-with-reason,
explicit amounts, dates resolved from Mola's own words, and an audit entry for every write."""
import json
import random
import re
import string
import time
from datetime import date, datetime, timedelta, timezone
from . import db

STAGES = ["New Lead", "Contacted", "Qualified", "Offer Made", "Under Contract", "Buyer Secured", "Closing Scheduled", "Closed"]
SIGNED = ["Under Contract", "Buyer Secured", "Closing Scheduled"]
OLD_STAGE = {"new lead": "New Lead", "contacted": "Contacted", "appointment": "Qualified", "qualified": "Qualified", "offer sent": "Offer Made",
             "offer made": "Offer Made", "under contract": "Under Contract", "assigned": "Buyer Secured", "buyer secured": "Buyer Secured",
             "closing scheduled": "Closing Scheduled", "closed": "Closed"}
PRIORITY = {"High": 0, "Normal": 1, "Low": 2}
OUTCOMES = {"no_answer": "No answer", "talked": "Talked", "not_now": "Not now", "dead": "Dead"}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class NeedInput(Exception):
    pass


def ts():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def today():
    return date.today().isoformat()


def uid(p=""):
    return p + format(int(time.time() * 1000), "x")[-8:] + "".join(random.choices(string.ascii_lowercase + string.digits, k=5))


def num(v):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v or "")) or 0)
    except ValueError:
        return 0.0


def has(v):
    return v is not None and str(v).strip() != ""


def norm_deal(d):
    d = dict(d)
    st = str(d.get("stage") or "").lower()
    if st == "dead":
        d["stage"], d["archived"] = "New Lead", True
        d.setdefault("archiveReason", "Marked dead (earlier version)")
    else:
        d["stage"] = OLD_STAGE.get(st, d.get("stage") if d.get("stage") in STAGES else "New Lead")
    return d


# ---------- guards: Mola's own words decide amounts and dates ----------
def _n(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def said_by_user(said, ctx):
    n = _n(said)
    return bool(n) and any(n in _n(t) for t in (ctx or {}).get("user_texts", []))


def amount_explicit(said):
    s = str(said or "").lower()
    return bool(re.search(r"\d{1,3}(,\d{3})+", s) or re.search(r"\d{4,}", re.sub(r"[$,\s]", "", s))
                or re.search(r"\d+(\.\d+)?\s*(k|m|mm|mil|million|thousand|grand)\b", s) or re.search(r"\b(thousand|million|grand)\b", s))


def check_amount(field, value, said, ctx):
    if ctx is None:
        return
    if not has(said):
        raise ValueError(f"amounts_said.{field} is required: pass Mola's exact words for the {field}.")
    if not said_by_user(said, ctx) or not amount_explicit(said):
        raise NeedInput(f"Just to confirm, the {field} is ${value:,.0f}?")


def resolve_date(phrase, base=None):
    base = base or date.today()
    p = re.sub(r"\s+", " ", re.sub(r"[.,!]", " ", str(phrase or "").lower())).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p):
        return p
    if re.search(r"\b(today|tonight)\b", p):
        return base.isoformat()
    if "day after tomorrow" in p:
        return (base + timedelta(days=2)).isoformat()
    if "tomorrow" in p:
        return (base + timedelta(days=1)).isoformat()
    m = re.search(r"\bin (\d+|one|two|three|four|five|six|seven|ten|a) (day|days|week|weeks)\b", p)
    if m:
        words = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}
        n = int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
        return (base + timedelta(days=n * (7 if m.group(2).startswith("week") else 1))).isoformat()
    for i, d in enumerate(DAYS):
        if re.search(rf"\b{d}s?\b", p):
            ahead = (i - base.weekday()) % 7
            this_one = base + timedelta(days=ahead or 7)
            if "next" in p:
                if ahead == 0:
                    return (base + timedelta(days=7)).isoformat()
                raise NeedInput(f"This {d.title()} ({this_one:%b %d}) or the following week ({this_one + timedelta(days=7):%b %d})?")
            if ahead == 0:
                raise NeedInput(f"Today or next {d.title()}?")
            return this_one.isoformat()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", p)
    if m:
        y = int(m.group(3)) if m.group(3) else base.year
        y = y + 2000 if y < 100 else y
        dt = date(y, int(m.group(1)), int(m.group(2)))
        if not m.group(3) and dt < base:
            dt = date(y + 1, dt.month, dt.day)
        return dt.isoformat()
    raise NeedInput(f'I couldn\'t work out the date from "{phrase}". What date should I use?')


def check_date(field, said, ctx):
    if ctx is None:
        return None
    if not has(said):
        raise ValueError(f"dates_said.{field} is required: pass Mola's exact words for the date.")
    if not said_by_user(said, ctx):
        raise NeedInput(f"Which date should I use for the {field}?")
    return resolve_date(said)


# ---------- storage + audit ----------
def items(con, kind):
    out = [json.loads(r["data"]) for r in con.execute("SELECT data FROM crm_items WHERE kind=? AND deleted=0", (kind,))]
    return [norm_deal(d) for d in out] if kind == "deals" else out


def get(con, kind, id_):
    r = con.execute("SELECT data FROM crm_items WHERE kind=? AND id=? AND deleted=0", (kind, id_)).fetchone()
    if not r:
        return None
    d = json.loads(r["data"])
    return norm_deal(d) if kind == "deals" else d


def put(con, kind, data, changes=None):
    before = get(con, kind, data["id"])
    data["_ts"] = ts()
    con.execute("""INSERT INTO crm_items(kind,id,data,ts,deleted) VALUES(?,?,?,?,0)
                   ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data, ts=excluded.ts, deleted=0""",
                (kind, data["id"], json.dumps(data), data["_ts"]))
    con.commit()
    if changes is not None:
        diff = {k: [(before or {}).get(k), data.get(k)] for k in set(data) | set(before or {})
                if k not in ("_ts", "updatedAt") and (before or {}).get(k) != data.get(k)}
        changes.append({"kind": kind, "id": data["id"], "name": data.get("address") or data.get("title") or data.get("name") or data["id"],
                        "action": "updated" if before else "created", "diff": diff})
    return data


def audit(con, op, label, request, status, summary, changes, error=""):
    a = {"id": "a_" + uid(), "ts": ts(), "finishedAt": ts(), "source": "desktop", "request": request or "", "op": op, "label": label,
         "describe": label, "status": status, "summary": summary, "error": error, "changes": changes}
    put(con, "audit", a)


def settings(con):
    s = get(con, "settings", "settings") or {}
    rules = s.get("rules") or {}
    return {"goal": num(s.get("goal")) or 50000, "defaultFee": num(s.get("defaultFee")) or 10000, "maoPct": num(s.get("maoPct")) or 70,
            "rehab": s.get("rehab") or {"light": 20, "medium": 35, "heavy": 60}, "avgFeeAssumption": num(s.get("avgFeeAssumption")),
            "noAnswer": rules.get("noAnswer") or [1, 3, 7], "talked": rules.get("talked") or 3, "notNow": rules.get("notNow") or 30,
            "emergencyStop": bool(s.get("emergencyStop"))}


# ---------- sync (called by the app through MCP) ----------
def pull(con, since=""):
    now = ts()
    rows = con.execute("SELECT kind,id,data,ts,deleted FROM crm_items WHERE ts>? AND ts<=?", (since or "", now)).fetchall()
    return {"now": now, "items": [{"kind": r["kind"], "id": r["id"], "ts": r["ts"], "deleted": bool(r["deleted"]),
                                   "data": None if r["deleted"] else json.loads(r["data"])} for r in rows]}


def push(con, items_in, deletes):
    applied = 0
    for it in items_in or []:
        kind, id_, data = it.get("kind"), it.get("id"), it.get("data")
        if not kind or not id_ or not isinstance(data, dict):
            continue
        t = it.get("ts") or data.get("_ts") or ""
        cur = con.execute("SELECT ts FROM crm_items WHERE kind=? AND id=?", (kind, id_)).fetchone()
        if cur and cur["ts"] and t and cur["ts"] > t:
            continue
        con.execute("""INSERT INTO crm_items(kind,id,data,ts,deleted) VALUES(?,?,?,?,0)
                       ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data, ts=excluded.ts, deleted=0""",
                    (kind, id_, json.dumps(data), t))
        applied += 1
    for d in deletes or []:
        t = d.get("ts") or ts()
        cur = con.execute("SELECT ts FROM crm_items WHERE kind=? AND id=?", (d.get("kind"), d.get("id"))).fetchone()
        if cur and cur["ts"] and cur["ts"] > t:
            continue
        con.execute("UPDATE crm_items SET deleted=1, ts=? WHERE kind=? AND id=?", (t, d.get("kind"), d.get("id")))
        applied += 1
    con.commit()
    return {"applied": applied, "now": ts()}


# ---------- read models ----------
def compact(d):
    keys = ["id", "address", "county", "stage", "archived", "seller", "phone", "nextFollowUp", "beds", "baths", "sqft", "asking", "price",
            "arv", "repairs", "fee", "actualFee", "condition", "touches", "lastContact", "optOut"]
    out = {k: d.get(k) for k in keys if d.get(k) not in (None, "", False)}
    if d.get("notes"):
        out["notes"] = d["notes"][-300:]
    return out


def queue(con):
    t = today()
    q = [d for d in items(con, "deals") if not d.get("archived") and d.get("stage") != "Closed" and
         ((d.get("nextFollowUp") and d["nextFollowUp"] <= t) or (not d.get("nextFollowUp") and not d.get("lastContact") and d.get("stage") == "New Lead"))]
    return sorted(q, key=lambda d: (not (d.get("nextFollowUp") and d["nextFollowUp"] < t), PRIORITY.get(d.get("priority"), 1),
                                    -STAGES.index(d.get("stage", "New Lead")), d.get("nextFollowUp") or ""))


def overview(con):
    t, ym, s = today(), today()[:7], settings(con)
    deals = [d for d in items(con, "deals") if not d.get("archived")]
    closed = [d for d in deals if d.get("stage") == "Closed"]
    month = [d for d in closed if (d.get("closedAt") or "")[:7] == ym]
    collected = sum(num(d.get("actualFee")) for d in month)
    hist = [num(d.get("actualFee")) for d in closed if num(d.get("actualFee")) > 0]
    avg = (sum(hist) / len(hist)) if hist else s["avgFeeAssumption"]
    remaining = max(0, s["goal"] - collected)
    return {"today": t, "goal": s["goal"], "collectedThisMonth": collected,
            "projectedNotEarned": sum(num(d.get("fee")) for d in deals if d.get("stage") in SIGNED),
            "remaining": remaining, "avgFee": round(avg), "avgFeeSource": "closed deals" if hist else ("assumption" if avg else "none"),
            "dealsNeeded": (-(-remaining // avg) if avg and remaining else None),
            "countsByStage": {st: sum(1 for d in deals if d.get("stage") == st) for st in STAGES},
            "queue": [compact(d) for d in queue(con)[:10]],
            "overdueTasks": [x for x in items(con, "tasks") if not x.get("done") and x.get("due") and x["due"] < t][:10]}


def _addr_key(a):
    s = re.sub(r"[^a-z0-9 ]", " ", str(a or "").lower().split(",")[0])
    for f, x in (("street", "st"), ("avenue", "ave"), ("road", "rd"), ("drive", "dr"), ("lane", "ln"), ("boulevard", "blvd"), ("court", "ct")):
        s = re.sub(rf"\b{f}\b", x, s)
    return re.sub(r"\s+", " ", s).strip()


def search_deals(con, query="", stage=""):
    q = (query or "").lower()
    k = _addr_key(q)
    out = []
    for d in items(con, "deals"):
        if stage and d.get("stage") != stage:
            continue
        hay = " ".join(str(d.get(x, "")) for x in ("address", "seller", "county", "phone")).lower()
        if not q or q in hay or (k and k in _addr_key(d.get("address"))):
            out.append(d)
    return {"count": len(out), "deals": [compact(d) for d in out[:20]]}


def _deal(con, id_):
    d = get(con, "deals", id_)
    if not d:
        raise ValueError("No deal with that id. Use search_deals first.")
    return d


def _sync_fu(con, d, changes, title=None):
    tid = "fu_" + d["id"]
    cur = get(con, "tasks", tid)
    if d.get("nextFollowUp") and not d.get("archived"):
        t = dict(cur or {"createdAt": ts(), "type": "Follow-up"})
        t.update(id=tid, dealId=d["id"], due=d["nextFollowUp"], done=False,
                 title=title or (cur or {}).get("title") or f"Follow up: {d.get('address', '').split(',')[0]}")
        put(con, "tasks", t, changes)
    elif cur and not cur.get("done"):
        cur.update(done=True, doneAt=today(), cancelled=True)
        put(con, "tasks", cur, changes)


DEAL_FIELDS = ["address", "county", "state", "seller", "phone", "sellerEmail", "propertyType", "beds", "baths", "sqft", "asking", "price",
               "arv", "repairs", "fee", "motivation", "timeline", "occupancy", "condition", "source", "priority"]
MONEY = {"asking", "price", "arv", "repairs", "fee", "actualFee"}


def _clean(fields, ctx, amounts_said):
    out = {}
    for k, v in (fields or {}).items():
        if k not in DEAL_FIELDS or v in (None, ""):
            continue
        if k in MONEY:
            v = num(v)
            check_amount(k, v, (amounts_said or {}).get(k), ctx)
        elif k in ("beds", "baths", "sqft"):
            v = num(v)
        out[k] = v
    return out


# ---------- validated write operations ----------
def _op(con, op, label, request, fn):
    changes = []
    try:
        summary, result = fn(changes)
        audit(con, op, label, request, "ok", summary, changes)
        return {"status": "ok", "summary": summary, **(result or {})}
    except NeedInput as e:
        return {"status": "needs_input", "question": str(e)}
    except Exception as e:
        if changes:
            audit(con, op, label, request, "failed", f"{label} failed: {e}", changes, str(e))
        return {"status": "error", "message": str(e)}


def create_lead(con, address, followUpDate="", amounts_said=None, dates_said=None, _ctx=None, **fields):
    def go(ch):
        if not has(address) or not re.search(r"\d", address.split(",")[0]):
            raise NeedInput("What's the full street address?")
        dup = [d for d in items(con, "deals") if _addr_key(d.get("address")) == _addr_key(address)]
        if dup:
            raise NeedInput(f"{dup[0]['address']} is already in the pipeline ({dup[0].get('stage')}). Update that one instead?")
        f = _clean(fields, _ctx, amounts_said)
        fu = check_date("followUp", (dates_said or {}).get("followUp"), _ctx) if has(followUpDate) else ""
        d = {"id": uid("d_"), "address": address.strip(), "stage": "New Lead", "priority": "Normal", "outreachPermission": "Unknown",
             "optOut": False, "tags": [], "createdAt": ts(), "updatedAt": ts(), **f}
        if fields.get("notes"):
            d["notes"] = f"[{today()}] {fields['notes']}"
        if fu:
            d["nextFollowUp"] = fu
        put(con, "deals", d, ch)
        _sync_fu(con, d, ch)
        return f"Added {d['address']}" + (f", follow-up {fu}" if fu else "") + ".", {"id": d["id"]}
    return _op(con, "create_lead", "Add lead", (_ctx or {}).get("request"), go)


def update_deal(con, id, amounts_said=None, notes="", _ctx=None, **fields):
    def go(ch):
        d = _deal(con, id)
        if "stage" in fields:
            raise ValueError("Use move_stage to change the stage.")
        f = _clean(fields, _ctx, amounts_said)
        d.update(f)
        if notes:
            d["notes"] = ((d.get("notes") or "") + f"\n[{today()}] {notes}").strip()
        d["updatedAt"] = ts()
        put(con, "deals", d, ch)
        return f"Updated {d['address']}.", {}
    return _op(con, "update_deal", "Update deal", (_ctx or {}).get("request"), go)


def move_stage(con, id, stage, actualFee=None, amounts_said=None, _ctx=None):
    def go(ch):
        d = _deal(con, id)
        st = next((s for s in STAGES if s.lower() == str(stage).lower()), None)
        if not st:
            raise ValueError(f"Stage must be one of {', '.join(STAGES)}.")
        if d.get("archived"):
            raise NeedInput(f"{d['address']} is archived. Restore it in the app first?")
        if st == "Closed":
            if actualFee is None:
                raise NeedInput(f"What fee did you actually collect on {d['address']}?")
            check_amount("actualFee", num(actualFee), (amounts_said or {}).get("actualFee"), _ctx)
            d.update(actualFee=num(actualFee), closedAt=today(), nextFollowUp="")
        d.update(stage=st, updatedAt=ts())
        put(con, "deals", d, ch)
        if st == "Closed":
            _sync_fu(con, d, ch)
        return f"Moved {d['address']} to {st}.", {}
    return _op(con, "move_stage", "Move stage", (_ctx or {}).get("request"), go)


def log_call(con, dealId, outcome, note="", reason="", followUpDate="", dates_said=None, _ctx=None):
    def go(ch):
        d = _deal(con, dealId)
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
        if outcome == "dead" and not has(reason):
            raise NeedInput(f"What's the reason {d['address']} is dead?")
        s = settings(con)
        streak = (d.get("noAnswerStreak") or 0) + 1 if outcome == "no_answer" else 0
        nxt = ""
        if outcome != "dead":
            if has(followUpDate):
                nxt = check_date("followUp", (dates_said or {}).get("followUp"), _ctx) or followUpDate
            else:
                gap = {"no_answer": s["noAnswer"][min(streak, len(s["noAnswer"])) - 1], "talked": s["talked"], "not_now": s["notNow"]}[outcome]
                nxt = (date.today() + timedelta(days=int(gap))).isoformat()
        put(con, "attempts", {"id": uid("att_"), "dealId": d["id"], "address": d["address"], "outcome": outcome, "note": note,
                              "reason": reason, "at": ts(), "day": today(), "attempt": (d.get("touches") or 0) + 1, "scheduled": nxt}, ch)
        d.update(touches=(d.get("touches") or 0) + 1, lastContact=today(), noAnswerStreak=streak, updatedAt=ts())
        if outcome in ("talked", "not_now") and d.get("stage") == "New Lead":
            d["stage"] = "Contacted"
        d["notes"] = ((d.get("notes") or "") + f"\n[{today()}] {OUTCOMES[outcome]}" + (f": {note or reason}" if (note or reason) else "")).strip()
        if outcome == "dead":
            d.update(archived=True, archiveReason=reason, archivedAt=today(), nextFollowUp="")
        else:
            d["nextFollowUp"] = nxt
        put(con, "deals", d, ch)
        _sync_fu(con, d, ch)
        return (f"Logged dead on {d['address']}; archived." if outcome == "dead"
                else f"Logged {OUTCOMES[outcome].lower()} on {d['address']}. Next follow-up {nxt}."), {}
    return _op(con, "log_call", "Log call outcome", (_ctx or {}).get("request"), go)


def create_task(con, title, due="", dealId="", dates_said=None, _ctx=None):
    def go(ch):
        if not has(title):
            raise NeedInput("What's the task?")
        if not has(due):
            raise NeedInput(f'When is "{title}" due?')
        d = check_date("due", (dates_said or {}).get("due"), _ctx) or due
        t = {"id": uid("t_"), "title": title, "type": "Other", "due": d, "dealId": dealId or None, "done": False, "createdAt": ts()}
        put(con, "tasks", t, ch)
        return f'Scheduled "{title}" for {d}.', {"id": t["id"]}
    return _op(con, "create_task", "Create task", (_ctx or {}).get("request"), go)


def list_tasks(con, includeDone=False):
    return [t for t in items(con, "tasks") if includeDone or not t.get("done")][:40]


def complete_task(con, id, _ctx=None):
    def go(ch):
        t = get(con, "tasks", id)
        if not t:
            raise ValueError("No task with that id")
        t.update(done=True, doneAt=today())
        put(con, "tasks", t, ch)
        return f'Completed "{t.get("title")}".', {}
    return _op(con, "complete_task", "Complete task", (_ctx or {}).get("request"), go)


def create_buyer(con, name, amounts_said=None, _ctx=None, **f):
    def go(ch):
        b = {"id": uid("b_"), "name": name, "verified": False, "createdAt": ts()}
        for k in ("company", "phone", "email", "rehabTolerance", "strategy", "notes"):
            if has(f.get(k)):
                b[k] = f[k]
        for k in ("markets", "propertyTypes"):
            if f.get(k):
                b[k] = f[k] if isinstance(f[k], list) else [x.strip() for x in str(f[k]).split(",") if x.strip()]
        if has(f.get("maxPrice")):
            check_amount("maxPrice", num(f["maxPrice"]), (amounts_said or {}).get("maxPrice"), _ctx)
            b["maxPrice"] = num(f["maxPrice"])
        put(con, "buyers", b, ch)
        return f"Added buyer {name}.", {"id": b["id"]}
    return _op(con, "create_buyer", "Add buyer", (_ctx or {}).get("request"), go)


def match_buyers(con, dealId):
    d = _deal(con, dealId)
    price = (num(d.get("price")) + num(d.get("fee"))) if num(d.get("price")) and num(d.get("fee")) else (num(d.get("price")) or num(d.get("asking")))
    hay = f"{d.get('address', '')} {d.get('county', '')}".lower()
    out = []
    for b in items(con, "buyers"):
        markets = [m.lower() for m in (b.get("markets") or [])]
        checks = []
        checks.append(("market", "unknown" if not markets else "pass" if any(m.replace(" county", "") in hay for m in markets) else "fail"))
        checks.append(("price", "unknown" if not has(b.get("maxPrice")) or not price else "pass" if price <= num(b.get("maxPrice")) else "fail"))
        fit = "no" if any(c[1] == "fail" for c in checks) else "partial" if any(c[1] == "unknown" for c in checks) else "match"
        if fit != "no":
            out.append({"name": b.get("name"), "fit": fit, "verified": bool(b.get("verified")), "checks": dict(checks)})
    return out


def analyze_deal(con, arv, sqft=0, tier="medium", repairs=None, maoPct=None, fee=None, asking=0):
    s = settings(con)
    rehab = num(repairs) if repairs not in (None, "") else num(sqft) * num(s["rehab"].get(tier, 35))
    pct = num(maoPct) or s["maoPct"]
    fee = s["defaultFee"] if fee in (None, "") else num(fee)
    buyer_max = num(arv) * pct / 100 - rehab
    return {"arv": num(arv), "rehab": round(rehab), "pct": pct, "fee": fee, "buyerMax": round(buyer_max), "mao": round(buyer_max - fee),
            "roomVsAsking": round(buyer_max - fee - num(asking)) if num(asking) else None}
