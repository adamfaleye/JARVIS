"""MCP server so Claude Desktop (and JARVIS) can talk to the engine. See README › Connect to Claude Desktop."""
import subprocess
import sys
from pathlib import Path
import traceback

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "mcp_server.log"
try:  # keep a log so startup problems are visible (Claude Desktop hides stderr)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _log = open(LOG, "a", encoding="utf-8", buffering=1)
except Exception:
    _log = None


def _note(msg):
    if _log:
        from datetime import datetime
        _log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


try:
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server
    except ModuleNotFoundError:  # mcp 2.x renamed it
        from mcp.server.mcpserver import MCPServer as _Server
    try:
        from mcp.types import ToolAnnotations
    except Exception:
        from mcp_types import ToolAnnotations
    READ_ONLY = {"annotations": ToolAnnotations(readOnlyHint=True)}
except Exception:
    _note("startup failed:\n" + traceback.format_exc())
    raise
from engine import db, config, sms, crm
from engine import scrape as scr

mcp = _Server("jarvis-engine")


@mcp.tool(**READ_ONLY)
def crm_pull(since: str = "", include_leads: bool = False, include_voice: bool = False, hunter: dict | None = None) -> dict:
    """JARVIS app sync: CRM items (deals, buyers, tasks, contracts, settings) changed after `since` (ISO timestamp).
    With include_leads, also returns county auction leads waiting for review (countyLeads).
    With include_voice, also returns the desktop voice state (voice: state, turn, heard, reply, online)."""
    con = db.connect()
    out = crm.pull(con, since) if since != "9999" else {"now": crm.ts(), "items": []}
    if include_leads:
        out["countyLeads"] = list_new_leads(500)
    if include_voice:
        out["voice"] = _voice_state(con)
    if hunter:
        out["hunter"] = _hunter(hunter, write=False)
    return out


def _hunter(req, write):
    op = (req or {}).get("op") or ""
    if op.startswith("hq."):  # MolaBucks Capital HQ: agents, tasks, goals, approvals, reports
        from engine import hq as q
        sub = op[3:]
        if (sub in q.WRITE_OPS) != write or (sub not in q.WRITE_OPS and sub not in q.READ_OPS):
            return {"error": f"'{op}' isn't allowed here."}
        try:
            qcon = q.connect()
            return {"ok": True, "data": q.handle(qcon, config.load(), dict(req, op=sub))}
        except Exception as e:
            return {"error": str(e) if isinstance(e, (ValueError, KeyError)) else f"{e.__class__.__name__}: {e}"}
    if op.startswith("out."):  # outreach: campaigns, numbers, dialer, call log, DNC, usage
        from engine import outreach as o
        sub = op[4:]
        if (sub in o.WRITE_OPS) != write or (sub not in o.WRITE_OPS and sub not in o.READ_OPS):
            return {"error": f"'{op}' isn't allowed here."}
        try:
            ocon = o.connect()
            o.legacy_migrate(ocon, config.load())
            return {"ok": True, "data": o.handle(ocon, config.load(), dict(req, op=sub))}
        except Exception as e:
            return {"error": str(e) if isinstance(e, (ValueError, KeyError)) else f"{e.__class__.__name__}: {e}"}
    from engine import hunter as h
    if (op in h.WRITE_OPS) != write:
        return {"error": f"'{op}' isn't allowed here."}
    try:
        hcon = h.connect()
        h.sync_registry(hcon, config.load())
        return {"ok": True, "data": h.handle(hcon, config.load(), req)}
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}" if not isinstance(e, (ValueError, KeyError)) else str(e)}


def _voice_state(con):
    import json as _json
    from datetime import datetime, timezone
    try:
        v = _json.loads(db.kv_get(con, "voice_state", "{}") or "{}")
    except Exception:
        v = {}
    age = None
    if v.get("at"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(v["at"].replace("Z", "+00:00"))).total_seconds()
        except Exception:
            age = None
    v["online"] = age is not None and age < 30
    return v


@mcp.tool()
def crm_push(items: list[dict] = [], deletes: list[dict] = [], lead_marks: dict | None = None, voice_cmd: dict | None = None,
             hunter: dict | None = None) -> dict:
    """JARVIS app sync: save CRM items changed in the app. items: [{kind,id,ts,data}], deletes: [{kind,id,ts}]. Newer timestamp wins.
    lead_marks: {ids: [lead ids], status: "synced" | "ignored"} removes reviewed county leads from the review list.
    voice_cmd: {action: "listen"} asks the desktop voice to listen on this PC's microphone now."""
    con = db.connect()
    out = crm.push(con, items, deletes) if (items or deletes) else {"applied": 0, "now": crm.ts()}
    if voice_cmd and voice_cmd.get("action") == "listen":
        import json as _json, time as _time, uuid as _uuid
        db.kv_set(con, "voice_cmd", _json.dumps({"action": "listen", "id": _uuid.uuid4().hex, "t": _time.time()}))
        out["voice"] = {"queued": True, **_voice_state(con)}
    if hunter:
        out["hunter"] = _hunter(hunter, write=True)
    if lead_marks:
        out["leads"] = mark_leads([int(i) for i in lead_marks.get("ids") or []], str(lead_marks.get("status") or "synced"))
    return out


@mcp.tool()
def list_new_leads(limit: int = 200, county: str = "") -> list[dict]:
    """New county-record leads not yet sent to JARVIS, newest sale/filing dates first. Optional county filter."""
    con = db.connect()
    q = "SELECT id,county,state,source_type,case_number,parcel_id,address,city,zip,owner,judgment_amount,assessed_value,event_date,url FROM leads WHERE status IN ('new','exported')"
    args = []
    if county:
        q += " AND county LIKE ?"
        args.append(f"%{county}%")
    q += " ORDER BY event_date LIMIT ?"
    args.append(max(1, min(int(limit or 200), 1000)))
    return [dict(r) for r in con.execute(q, args)]


@mcp.tool()
def mark_leads(ids: list[int], status: str = "synced") -> dict:
    """Mark leads as synced (added to the JARVIS pipeline) or ignored, so they leave the review list."""
    if status not in ("synced", "ignored", "new"):
        return {"updated": 0, "error": "status must be synced, ignored or new"}
    con = db.connect()
    con.executemany("UPDATE leads SET status=? WHERE id=?", [(status, i) for i in ids])
    con.commit()
    return {"updated": len(ids), "status": status}


@mcp.tool()
def list_counties() -> list[dict]:
    """County sources being scraped, plus the last run result for each."""
    con = db.connect()
    out = []
    for c in config.load_counties():
        label = f"{c['name']} {c.get('state','')} ({c.get('sale_type') or c.get('adapter')})"
        r = con.execute("SELECT finished,found,new,error FROM runs WHERE target=? ORDER BY id DESC LIMIT 1", (label,)).fetchone()
        out.append({**c, "last_run": dict(r) if r else None})
    return out


@mcp.tool()
def add_county(name: str, state: str, url: str = "", instructions: str = "") -> dict:
    """Start scraping a county. With no URL, auto-detects its RealAuction foreclosure/tax-deed (and Ohio sheriff sale) sites.
    With a URL (e.g. a clerk's foreclosure or lis pendens list), Claude reads that page using the instructions."""
    added = scr.add_county(name, state, url or None, None, instructions or None, log=lambda *_: None)
    if not added:
        return {"added": [], "note": "No auction site found automatically. Provide the county's records page URL."}
    return {"added": added}


@mcp.tool()
def run_scrape(county: str = "") -> str:
    """Run the scraper now (all counties, or one by name). Takes a few minutes."""
    args = [sys.executable, str(ROOT / "run.py"), "scrape"] + (["--county", county] if county else [])
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    return (r.stdout + r.stderr)[-6000:]


@mcp.tool()
def sms_overview() -> dict:
    """Texting status: contacts by status, texts sent today, latest replies."""
    return sms.overview()


@mcp.tool()
def feed_matches(limit: int = 30) -> list[dict]:
    """Latest WhatsApp feed deals with matched buyers."""
    con = db.connect()
    return [dict(r) for r in con.execute("SELECT * FROM feed_deals ORDER BY id DESC LIMIT ?", (limit,))]


if __name__ == "__main__":
    _note(f"starting (python {sys.version.split()[0]})")
    try:
        mcp.run()
    except Exception:
        _note("crashed:\n" + traceback.format_exc())
        raise
