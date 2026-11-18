"""Demo page for a booked AI Sidekicks call: one self-contained HTML page showing what an AI assistant would do for that
specific business (their services, their kind of callers, when it hands off to a person), to screen-share on the call.

Only made once a call is booked (automatically, alongside the pre-call brief) or when you ask for one. Built from facts
JARVIS already has: the CRM record (sourced facts), the pre-call brief and the meeting. The AI (fast tier, priced against
the daily cap, a few cents) writes the content as JSON; a fixed template turns it into the page, so the page can't run
anything or load anything from the internet. It says plainly that it's a demo with sample conversations, not a live
system, and anything not known (hours, prices, service area) is shown as "set up with you".
Pages are saved under data/demos/. Nothing here contacts anyone.
"""
import html as _html
import json
import re
from datetime import datetime

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS hq_demo_pages(key TEXT PRIMARY KEY, company TEXT, prospect_id TEXT, business_id TEXT, meeting_start TEXT,
  path TEXT, created TEXT, content TEXT, cost REAL, status TEXT, error TEXT);
"""
MAX_PER_DAY = 4
SYSTEM = ("You design a short, honest demo of a custom AI assistant that AI Sidekicks would build for one specific small business, "
          "to show on a sales call. AI Sidekicks builds custom AI assistants (calls, messages, booking, follow-ups, CRM updates, with a person in the loop) "
          "and custom CRMs. Use only facts in the data. Never invent prices, hours, staff names, reviews, results or software they use.")
OUTPUT = ('{"headline": "what this assistant does for them, under 12 words", "business_line": "one sentence on what they do, from the facts", '
          '"greeting": "how the assistant answers the phone for them (says it is their virtual assistant)", '
          '"scenarios": [{"title": "short", "caller_need": "one sentence", "conversation": [{"who": "Caller" or "Assistant", "text": "one or two sentences"}], '
          '"what_happens_next": ["what the assistant does after the call: e.g. books the slot, texts a confirmation, logs it in the CRM, alerts the on-call tech"]}], '
          '"handoffs": ["when it passes to a person, and how"], "would_connect_to": [{"system": "name", "why": "short", "known": true only if the facts say they use it}], '
          '"set_up_with_you": ["what we would configure with them: hours, service area, prices, emergency rules..."], '
          '"not_included": ["what the assistant will not do, e.g. take card numbers, promise arrival times it cannot see"], "needs_info": []}')
INSTRUCTIONS = ("Write the demo content for this business. Give 3 scenarios that fit their real services and the kind of calls they would get "
                "(for a home-services trade include one urgent or after-hours call and how it reaches a person; for other industries pick the "
                "equivalent). Keep each conversation to 4-6 short turns. Where a fact isn't known (hours, prices, service area, booking system), "
                "the assistant says it would be set up for them, and it goes in set_up_with_you. It never quotes a price or arrival time it can't know, "
                "never takes card or Social Security numbers, and says early that it's a virtual assistant. Nothing here is a promise of results.")


def ensure(con):
    con.executescript(SCHEMA)
    con.commit()


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:50] or "demo"


def _key(prospect_id, business_id, company):
    return str(business_id or prospect_id or _slug(company))[:120]


def build(con, prospect_id=None, business_id=None, company=None, meeting=None, force=False, ask=None, out_dir=None, log=print):
    """Make (or remake, with force) the demo page for a company. Returns {status, path, ...}."""
    from . import agents_ai, hq, precall
    ensure(con)
    rec = hq.di_record_brief(con, prospect_id=prospect_id, business_id=business_id, limit=15)
    name = (rec.get("name") if rec else None) or str(company or "").strip()
    if not name:
        return {"status": "needs_input", "question": "Which company is the demo for?"}
    key = _key(prospect_id, rec.get("business_id") if rec else business_id, name)
    have = con.execute("SELECT * FROM hq_demo_pages WHERE key=?", (key,)).fetchone()
    if have and have["status"] == "ready" and not force:
        return {"status": "ok", "path": have["path"], "company": have["company"], "summary": f"Demo page already made for {have['company']}.", "existing": True}
    brief = None
    try:
        b = precall.get(con, prospect_id or name) if (prospect_id or name) else None
        brief = {k: b["brief"].get(k) for k in ("who_they_are", "size", "software", "current_setup", "likely_pain") if b["brief"].get(k)} if b and b.get("brief") else None
    except Exception:  # noqa: BLE001
        brief = None
    data = {"company": name, "crm_record": rec or {}, "precall_brief": brief or {}, "meeting": meeting or {}}
    try:
        out, meta = (ask or agents_ai.ask)(con, agent="architect", kind="architect.demo_page", task_id=f"demo:{key}", business="demigod", tier="fast",
                                           system=SYSTEM, instructions=INSTRUCTIONS, data=data, output_spec=OUTPUT, max_tokens=2600)
    except agents_ai.CapReached as e:
        _store(con, key, name, prospect_id, rec, meeting, None, None, 0, "waiting", str(e))
        return {"status": "waiting", "summary": str(e)}
    if not isinstance(out, dict) or not out.get("scenarios") or not out.get("greeting"):
        _store(con, key, name, prospect_id, rec, meeting, None, None, meta.get("cost", 0) if isinstance(meta, dict) else 0, "failed", "The AI's answer didn't come back as demo content.")
        return {"status": "failed", "summary": "The demo content didn't come back in the right shape; no page made."}
    shown = {k: out.get(k) for k in ("headline", "business_line", "greeting", "scenarios", "handoffs")}  # "won't guarantee X" in not_included is fine
    bad = agents_ai.claims_found(json.dumps(shown))
    if bad:
        _store(con, key, name, prospect_id, rec, meeting, None, out, meta.get("cost", 0), "failed", "Held back by the claims check: " + "; ".join(bad))
        return {"status": "failed", "summary": "The demo content made a claim we can't back up (" + "; ".join(bad) + "); no page made."}
    page = render(name, out, meeting)
    d = out_dir or (db.DB_PATH.parent / "demos")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_slug(name)}-{datetime.now():%Y%m%d}.html"
    path.write_text(page, encoding="utf-8")
    _store(con, key, name, prospect_id, rec, meeting, str(path), out, meta.get("cost", 0), "ready", "")
    log(f"Demo page ready for {name}: {path}")
    return {"status": "ok", "path": str(path), "company": name, "cost": meta.get("cost"), "summary": f"Demo page ready for {name}.",
            "needs_info": out.get("needs_info") or []}


def _store(con, key, name, prospect_id, rec, meeting, path, content, cost, status, err):
    con.execute("""INSERT INTO hq_demo_pages(key,company,prospect_id,business_id,meeting_start,path,created,content,cost,status,error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET company=excluded.company, path=COALESCE(excluded.path, path),
                   meeting_start=COALESCE(excluded.meeting_start, meeting_start), created=excluded.created, content=COALESCE(excluded.content, content),
                   cost=COALESCE(cost,0)+excluded.cost, status=excluded.status, error=excluded.error""",
                (key, name, prospect_id or "", (rec or {}).get("business_id") or "", (meeting or {}).get("start"), path, db.now(),
                 json.dumps(content) if content else None, cost or 0, status, err))
    con.commit()


def get(con, who):
    ensure(con)
    w = str(who or "").strip()
    if not w:
        return None
    r = con.execute("SELECT * FROM hq_demo_pages WHERE key=? OR prospect_id=? OR business_id=? OR lower(company) LIKE ? ORDER BY created DESC LIMIT 1",
                    (w, w, w, f"%{w.lower()}%")).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content"] = json.loads(d["content"]) if d.get("content") else None
    return d


def list_pages(con, limit=20):
    ensure(con)
    return {"pages": [{k: r[k] for k in ("key", "company", "meeting_start", "path", "created", "status", "error")}
                      for r in con.execute("SELECT * FROM hq_demo_pages ORDER BY created DESC LIMIT ?", (int(limit),))]}


# ------------------------------------------------------------------ the page (fixed template; every value escaped)
def _e(x):
    return _html.escape(str(x or ""), quote=True)


def _li(items):
    return "".join(f"<li>{_e(i)}</li>" for i in items or [] if str(i or "").strip())


def render(company, c, meeting=None):
    scen = []
    for s in (c.get("scenarios") or [])[:4]:
        turns = "".join(f'<div class="turn {"a" if str(t.get("who", "")).lower().startswith("assist") else "c"}"><span>{_e(t.get("who"))}</span><p>{_e(t.get("text"))}</p></div>'
                        for t in (s.get("conversation") or [])[:8] if isinstance(t, dict))
        nxt = _li(s.get("what_happens_next"))
        scen.append(f'<section class="card"><h3>{_e(s.get("title"))}</h3><p class="need">{_e(s.get("caller_need"))}</p>'
                    f'<div class="convo">{turns}</div>' + (f'<h4>Then the assistant</h4><ul>{nxt}</ul>' if nxt else "") + "</section>")
    conn = "".join(f'<li><b>{_e(x.get("system"))}</b>: {_e(x.get("why"))}{"" if x.get("known") else " <em>(to confirm)</em>"}</li>'
                   for x in c.get("would_connect_to") or [] if isinstance(x, dict) and x.get("system"))
    when = ""
    if meeting and meeting.get("start"):
        when = f'<p class="meta">Prepared for our call on {_e(str(meeting["start"]).replace("T", " at ")[:19])}</p>'
    blocks = [("When it hands off to your team", _li(c.get("handoffs"))), ("What we'd set up with you", _li(c.get("set_up_with_you"))),
              ("What it won't do", _li(c.get("not_included")))]
    side = "".join(f'<section class="card small"><h3>{t}</h3><ul>{b}</ul></section>' for t, b in blocks if b)
    if conn:
        side += f'<section class="card small"><h3>Connects to</h3><ul>{conn}</ul></section>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(company)} · AI assistant demo</title>
<style>
:root{{--ink:#12263F;--muted:#5b6472;--gold:#2563EB;--line:#dfe6f0;--bg:#FFF8F0;--card:#fff;--a:#EAF1FE}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}}
header{{background:var(--ink);color:#fff;padding:28px 20px}}header .in,main{{max-width:1040px;margin:0 auto}}
.tag{{display:inline-block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#e9d9b8;margin-bottom:8px}}
h1{{margin:0 0 6px;font-size:28px;line-height:1.2}}header p{{margin:4px 0;color:#d1d5db}}.meta{{font-size:14px}}
main{{padding:20px;display:grid;grid-template-columns:2fr 1fr;gap:18px}}@media(max-width:800px){{main{{grid-template-columns:1fr}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}}
.card h3{{margin:0 0 6px;font-size:18px}}.card h4{{margin:14px 0 4px;font-size:14px;color:var(--gold)}}.need{{color:var(--muted);margin:0 0 10px}}
.greet{{border-left:4px solid var(--gold)}}.turn{{display:flex;gap:10px;margin:8px 0}}.turn span{{flex:0 0 78px;font-size:12px;color:var(--muted);padding-top:3px}}
.turn p{{margin:0;padding:8px 12px;border-radius:8px;background:#f3f4f6}}.turn.a p{{background:var(--a)}}
ul{{margin:6px 0 0;padding-left:20px}}li{{margin:3px 0}}.small h3{{font-size:16px}}em{{color:var(--muted)}}
footer{{max-width:1040px;margin:0 auto;padding:0 20px 30px;color:var(--muted);font-size:13px}}
</style></head><body>
<header><div class="in"><div class="tag">Demo · AI Sidekicks</div><h1>{_e(c.get("headline") or "Your AI assistant")}</h1>
<p>{_e(company)}{" · " + _e(c.get("business_line")) if c.get("business_line") else ""}</p>{when}</div></header>
<main><div><section class="card greet"><h3>How it answers</h3><p>"{_e(c.get("greeting"))}"</p></section>{"".join(scen)}</div><div>{side}</div></main>
<footer>This is a demo prepared by AI Sidekicks with sample conversations written for {_e(company)}. It is not a live system and nothing here was sent to anyone.
Anything marked "to confirm" or listed under "set up with you" would be configured together before launch.</footer>
</body></html>
"""
