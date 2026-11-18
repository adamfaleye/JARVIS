"""Live voice calls with any employee (ElevenLabs voice agents), reaching JARVIS on this PC through a secure tunnel.

How it fits together:
  phone browser --(your private call link)--> tunnel --> this bridge --> signed ElevenLabs session (voice)
  ElevenLabs voice agent --(webhook tool, secret header)--> tunnel --> this bridge --> employees.talk() (same brain as Telegram)

Safety:
  * The bridge listens on 127.0.0.1 only; the public side is a Cloudflare tunnel (cloudflared) with HTTPS.
  * Two separate secrets, both made here and stored in data/voice_bridge.json (never in the app or chat):
    - owner_key: in your private call link; without it the call page and signed sessions are refused.
    - tool_key: sent by ElevenLabs as X-JARVIS-Key on every tool call; without it /employee answers nothing.
  * ElevenLabs agents are created with authentication on, so only a signed session (from your link) can start a call.
  * Employees keep their phone rules: look up, research, list, draft. Nothing outward without your approval.
"""
import json
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, db, org

PORT = 8765
STATE = config.ROOT / "data" / "voice_bridge.json"
XI = "https://api.elevenlabs.io"
URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
VOICE_AGENTS = ("jarvis", "sdr", "ae", "marketing", "architect", "hunter", "pm", "research", "finance", "chief_of_staff")
HTTP = None  # tests replace this: fn(method, url, body, headers) -> dict


def state():
    p = Path(STATE)
    s = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    changed = False
    for k in ("owner_key", "tool_key"):
        if not s.get(k):
            s[k] = secrets.token_urlsafe(24); changed = True
    s.setdefault("agents", {})
    if changed:
        save(s)
    return s


def save(s):
    p = Path(STATE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _xi(method, path, body=None):
    import os
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY isn't set in .env")
    hdr = {"xi-api-key": key, "Content-Type": "application/json"}
    if HTTP:
        return HTTP(method, XI + path, body, hdr)
    req = urllib.request.Request(XI + path, data=json.dumps(body).encode() if body is not None else None, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


# ------------------------------------------------------------------ ElevenLabs setup (run once; tool URLs follow the tunnel)
def _tool_body(aid, base, tool_key):
    r = org.ROSTER[aid]
    return {"tool_config": {
        "type": "webhook", "name": "ask_jarvis",
        "description": f"Look things up in Mola's real records or take an action in your lane as {r['name']}. Pass Mola's request in plain words. "
                       "Use it before answering anything about prospects, numbers, status or tasks. It may take a few seconds.",
        "response_timeout_secs": 60,
        "api_schema": {"url": f"{base}/employee/{aid}", "method": "POST",
                       "request_body_schema": {"type": "object", "properties": {"request": {"type": "string", "description": "What Mola wants, in his words"}}, "required": ["request"]},
                       "request_headers": {"X-JARVIS-Key": tool_key}}}}


def _prompt(aid):
    from . import employees
    return (employees.system_prompt(aid) + " This is a live voice call. Talk naturally and briefly, one or two sentences at a time. "
            "For anything about records, prospects, tasks or actions, call ask_jarvis with Mola's request and tell him the answer in your own words. "
            "Say 'one sec' before a lookup.")


def setup(base_url=None, voices=None, log=print):
    """Create (or refresh) one ElevenLabs voice agent per employee, each with an ask_jarvis tool pointed at the bridge."""
    s = state()
    base = (base_url or s.get("public_url") or "https://placeholder.invalid").rstrip("/")
    voices = voices or {}
    for aid in VOICE_AGENTS:
        a = s["agents"].get(aid) or {}
        body = _tool_body(aid, base, s["tool_key"])
        if a.get("tool_id"):
            _xi("PATCH", f"/v1/convai/tools/{a['tool_id']}", body)
        else:
            a["tool_id"] = _xi("POST", "/v1/convai/tools", body)["id"]
        conv = {"agent": {"first_message": f"{org.ROSTER[aid]['name']} here. What do you need?", "language": "en",
                          "prompt": {"prompt": _prompt(aid), "tool_ids": [a["tool_id"]]}}}
        if voices.get(aid):
            conv["tts"] = {"voice_id": voices[aid]}
        payload = {"name": f"AI Sidekicks · {org.ROSTER[aid]['name']}", "conversation_config": conv,
                   "platform_settings": {"auth": {"enable_auth": True}}, "tags": ["jarvis", "ai-sidekicks"]}
        if a.get("agent_id"):
            _xi("PATCH", f"/v1/convai/agents/{a['agent_id']}", payload)
        else:
            a["agent_id"] = _xi("POST", "/v1/convai/agents/create", payload)["agent_id"]
        s["agents"][aid] = a
        save(s)
        log(f"  voice agent ready: {org.ROSTER[aid]['name']}")
    return s["agents"]


def point_tools(base_url, log=print):
    """The tunnel's address changed: point every employee's ask_jarvis tool at it."""
    s = state()
    s["public_url"] = base_url.rstrip("/"); save(s)
    n = 0
    for aid, a in s["agents"].items():
        if a.get("tool_id"):
            _xi("PATCH", f"/v1/convai/tools/{a['tool_id']}", _tool_body(aid, s["public_url"], s["tool_key"]))
            n += 1
    log(f"[{datetime.now():%H:%M}] voice calls: {n} employee line(s) pointed at the tunnel")
    return n


def call_link(aid="jarvis"):
    s = state()
    if not s.get("public_url"):
        return None
    return f"{s['public_url']}/call/{aid}?k={s['owner_key']}"


# ------------------------------------------------------------------ the bridge (127.0.0.1 only)
CALL_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call {name}</title><style>body{{margin:0;font:16px system-ui;background:#12263F;color:#FFF8F0;display:grid;place-items:center;min-height:100vh;text-align:center}}
button{{font:600 20px system-ui;padding:18px 34px;border-radius:999px;border:0;background:#EA580C;color:#fff;margin:8px}}#s{{opacity:.8;margin-top:14px;min-height:1.5em}}
select{{font:16px system-ui;padding:8px;border-radius:8px;margin-bottom:18px}}</style></head><body><main>
<h1 style="font-weight:600">Call your team</h1><select id="who">{options}</select><br><button id="go">Start call</button><button id="end" hidden>End call</button><div id="s"></div></main>
<script type="module">
import {{ Conversation }} from "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.34.0/+esm";
const k = new URLSearchParams(location.search).get("k"); let conv = null; const s = document.getElementById("s");
document.getElementById("who").value = "{aid}";
document.getElementById("go").onclick = async () => {{
  try {{ s.textContent = "Connecting…"; await navigator.mediaDevices.getUserMedia({{ audio: true }});
    const who = document.getElementById("who").value;
    const r = await fetch(`/signed/${{who}}?k=${{encodeURIComponent(k)}}`); const j = await r.json(); if (!r.ok) throw new Error(j.error || "refused");
    conv = await Conversation.startSession({{ signedUrl: j.signed_url, onStatusChange: x => s.textContent = x.status, onError: e => s.textContent = String(e) }});
    document.getElementById("go").hidden = true; document.getElementById("end").hidden = false;
  }} catch (e) {{ s.textContent = "Couldn't start: " + e.message; }}
}};
document.getElementById("end").onclick = async () => {{ if (conv) await conv.endSession(); conv = null; s.textContent = "Call ended";
  document.getElementById("go").hidden = false; document.getElementById("end").hidden = true; }};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _owner_ok(self):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        k = dict(p.split("=", 1) for p in q.split("&") if "=" in p).get("k", "")
        return secrets.compare_digest(urllib.request.unquote(k), state()["owner_key"])

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        m = re.fullmatch(r"/(call|signed)/([a-z_]+)", path)
        if not m or m.group(2) not in VOICE_AGENTS:
            return self._send(404, {"error": "not found"})
        if not self._owner_ok():
            return self._send(403, {"error": "This link isn't valid."})
        aid = m.group(2)
        if m.group(1) == "call":
            opts = "".join(f'<option value="{a}">{org.ROSTER[a]["name"]}</option>' for a in VOICE_AGENTS if state()["agents"].get(a, {}).get("agent_id"))
            return self._send(200, CALL_PAGE.format(name=org.ROSTER[aid]["name"], aid=aid, options=opts), "text/html")
        agent_id = state()["agents"].get(aid, {}).get("agent_id")
        if not agent_id:
            return self._send(409, {"error": "That employee's voice line isn't set up yet."})
        try:
            r = _xi("GET", f"/v1/convai/conversation/get-signed-url?agent_id={agent_id}")
            return self._send(200, {"signed_url": r.get("signed_url")})
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"error": str(e)[:200]})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        m = re.fullmatch(r"/employee/([a-z_]+)", path)
        if not m or m.group(1) not in org.ROSTER:
            return self._send(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("X-JARVIS-Key") or "", state()["tool_key"]):
            return self._send(403, {"error": "refused"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            from . import employees, hq
            con = hq.connect()
            try:
                cfg = config.load()
            except Exception:  # noqa: BLE001
                cfg = {}
            r = employees.talk(con, cfg, m.group(1), str(body.get("request") or "")[:2000], source="owner_call")
            return self._send(200, {"answer": r["reply"]})
        except Exception as e:  # noqa: BLE001
            return self._send(200, {"answer": f"I couldn't get that right now ({str(e)[:120]})."})


def serve(port=PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # never 0.0.0.0: only the tunnel reaches it
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="voice-bridge", daemon=True).start()
    return srv


def find_cloudflared():
    import shutil
    import os
    for c in (shutil.which("cloudflared"), str(config.ROOT / "tools" / "cloudflared.exe"),
              os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"),
              r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"):
        if c and Path(c).exists():
            return c
    return None


def install_cloudflared():
    """Install Cloudflare's tunnel program with Windows' own package manager (owner-approved). Returns what happened."""
    if find_cloudflared():
        return {"installed": True, "note": "Already installed."}
    p = subprocess.run(["winget", "install", "--id", "Cloudflare.cloudflared", "-e", "--silent", "--accept-source-agreements", "--accept-package-agreements"],
                       capture_output=True, text=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"installed": bool(find_cloudflared()), "exit_code": p.returncode, "output": (p.stdout or p.stderr or "")[-800:]}


STATUS = config.ROOT / "data" / "voice_bridge_status.json"
LOGF = config.ROOT / "data" / "voice_bridge.log"


def tunnel_status():
    try:
        return json.loads(Path(STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _status(**kw):
    st = tunnel_status()
    st.update(kw, at=datetime.now().isoformat(timespec="seconds"))
    try:
        Path(STATUS).parent.mkdir(parents=True, exist_ok=True)
        Path(STATUS).write_text(json.dumps(st, indent=2), encoding="utf-8")
    except OSError:
        pass


def _filelog(log):
    def f(msg):
        log(msg)
        try:
            with open(LOGF, "a", encoding="utf-8") as fh:
                fh.write(msg.rstrip() + "\n")
        except OSError:
            pass
    return f


def run_forever(cfg, log=print):
    """Bridge + tunnel. Restarts the tunnel if it drops and re-points the voice agents at its new address.
    Writes what it's doing to data/voice_bridge_status.json and data/voice_bridge.log, so problems are visible."""
    log = _filelog(log)
    exe = find_cloudflared()
    if not exe:
        log(f"[{datetime.now():%H:%M}] voice calls: cloudflared isn't installed, so live calls are off (see README 9k)")
        _status(state="off", error="cloudflared isn't installed")
        return
    try:
        serve()
    except OSError as e:
        log(f"[{datetime.now():%H:%M}] voice calls: can't listen on 127.0.0.1:{PORT} ({e})")
        _status(state="error", error=f"port {PORT} is busy or blocked: {e}")
        raise
    _status(state="starting", error="", exe=exe)
    while True:
        tail = []
        try:
            p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            url = None
            for line in p.stdout:
                tail = (tail + [line.rstrip()])[-15:]
                m = URL_RE.search(line)
                if m and not url:
                    url = m.group(0)
                    log(f"[{datetime.now():%H:%M}] voice calls: tunnel up at {url}")
                    try:
                        point_tools(url, log)
                        _status(state="up", error="", url=url)
                    except Exception as e:  # noqa: BLE001
                        log(f"[{datetime.now():%H:%M}] voice calls: couldn't update ElevenLabs ({e})")
                        _status(state="up", error=f"couldn't update ElevenLabs: {e}", url=url)
            p.wait()
            err = " | ".join(x for x in tail[-4:] if x)[:400]
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        log(f"[{datetime.now():%H:%M}] voice calls: tunnel stopped ({err}); restarting in 30 s")
        for x in tail:
            log("    cloudflared: " + x)
        _status(state="restarting", error=err)
        time.sleep(30)
