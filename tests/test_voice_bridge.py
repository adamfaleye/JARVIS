"""Live voice calls: ElevenLabs agents with auth on, tools that follow the tunnel, and a local bridge that refuses
anyone without the right key. ElevenLabs is faked; the bridge runs for real on 127.0.0.1.
Run: python tests/test_voice_bridge.py"""
import json, os, sys, tempfile, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config
tmp = Path(tempfile.mkdtemp()); db.DB_PATH = tmp / "data" / "t.db"; (tmp / "data").mkdir(); config.ROOT = tmp
from engine import hq, voice_bridge, employees, telegram
voice_bridge.STATE = tmp / "data" / "voice_bridge.json"
os.environ["ELEVENLABS_API_KEY"] = "test-key"
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

calls = []
def fake_http(method, url, body, headers):
    calls.append((method, url.replace(voice_bridge.XI, ""), body, headers))
    if url.endswith("/v1/convai/tools"):
        return {"id": f"tool_{len(calls)}"}
    if url.endswith("/v1/convai/agents/create"):
        return {"agent_id": f"agent_{len(calls)}"}
    if "get-signed-url" in url:
        return {"signed_url": "wss://signed.example/abc"}
    return {}
voice_bridge.HTTP = fake_http

print("Setup")
s = voice_bridge.state()
ok(len(s["owner_key"]) > 20 and len(s["tool_key"]) > 20 and s["owner_key"] != s["tool_key"], "two separate random keys are made on the PC")
agents = voice_bridge.setup(voices={"sdr": "bIHbv24MWmeRgasZH58o"}, log=lambda *a: None)
ok(set(agents) == set(voice_bridge.VOICE_AGENTS) and all(a.get("agent_id") and a.get("tool_id") for a in agents.values()), "one voice agent + tool per employee")
creates = [c for c in calls if c[1] == "/v1/convai/agents/create"]
ok(all(c[2]["platform_settings"]["auth"]["enable_auth"] for c in creates), "every agent requires a signed session (no public link)")
sdr = next(c for c in creates if "SDR" in c[2]["name"])
ok(sdr[2]["conversation_config"]["tts"]["voice_id"] == "bIHbv24MWmeRgasZH58o", "the SDR speaks in its own voice")
ok("never send" in sdr[2]["conversation_config"]["agent"]["prompt"]["prompt"] and sdr[2]["conversation_config"]["agent"]["prompt"]["tool_ids"], "the voice agent keeps the same rules and uses the ask_jarvis tool")
tool = next(c for c in calls if c[1] == "/v1/convai/tools")
ok(tool[2]["tool_config"]["api_schema"]["request_headers"]["X-JARVIS-Key"] == s["tool_key"], "the tool carries the tool key, not your call key")
ok(all(c[3]["xi-api-key"] == "test-key" for c in calls), "the ElevenLabs key comes from .env on the PC")
n0 = len(calls)
voice_bridge.setup(log=lambda *a: None)
ok(not [c for c in calls[n0:] if c[0] == "POST"], "running setup again updates the same agents instead of making new ones")

print("Following the tunnel")
n0 = len(calls)
voice_bridge.point_tools("https://abc-def.trycloudflare.com", log=lambda *a: None)
patched = [c for c in calls[n0:] if c[0] == "PATCH"]
ok(len(patched) == len(voice_bridge.VOICE_AGENTS) and all(c[2]["tool_config"]["api_schema"]["url"].startswith("https://abc-def.trycloudflare.com/employee/") for c in patched), "a new tunnel address re-points every tool")
link = voice_bridge.call_link("sdr")
ok(link.startswith("https://abc-def.trycloudflare.com/call/sdr?k=") and s["owner_key"] in link, "your private call link")
ok(voice_bridge.URL_RE.search("INF |  https://quiet-fox-12.trycloudflare.com  |") is not None, "the tunnel address is read from cloudflared's output")

print("The bridge refuses strangers")
srv = voice_bridge.serve(port=0)
base = f"http://127.0.0.1:{srv.server_address[1]}"
ok(srv.server_address[0] == "127.0.0.1", "listens on this PC only")
def req(method, path, body=None, headers=None):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
ok(req("GET", "/call/sdr?k=wrong")[0] == 403, "a call link with the wrong key is refused")
code, html = req("GET", f"/call/sdr?k={s['owner_key']}")
ok(code == 200 and "Start call" in html and "SDR" in html, "the right link opens the call page")
code, body = req("GET", f"/signed/sdr?k={s['owner_key']}")
ok(code == 200 and json.loads(body)["signed_url"].startswith("wss://"), "it hands the page a short-lived signed session")
ok(req("POST", "/employee/sdr", {"request": "status?"})[0] == 403, "a tool call without the tool key is refused")
ok(req("POST", "/employee/sdr", {"request": "status?"}, {"X-JARVIS-Key": s["owner_key"]})[0] == 403, "your call key doesn't work as the tool key")
employees_talk = employees.talk
employees.talk = lambda con, cfg, aid, text, source=None, **k: {"reply": f"{aid} heard: {text} ({source})", "name": aid, "agent": aid}
code, body = req("POST", "/employee/sdr", {"request": "who replied today?"}, {"X-JARVIS-Key": s["tool_key"], "Content-Type": "application/json"})
ok(code == 200 and json.loads(body)["answer"] == "sdr heard: who replied today? (owner_call)", "with the tool key, the employee answers (same brain as Telegram)")
employees.talk = employees_talk
ok(req("GET", "/other")[0] == 404, "nothing else is served")
srv.shutdown()

print("Telegram hands you the link")
con = hq.connect()
ok("trycloudflare.com/call/ae?k=" in telegram.handle(con, {}, "call ae"), "'call ae' replies with your private link to the AE")
ok("Voice lines" in telegram.handle(con, {}, "call frontdesk"), "employees without a voice line are listed instead")


print("The tunnel reports what it's doing")
import threading, time as _t
fake = tmp / "fake_cloudflared.py"
fake.write_text("import sys\nprint('INF |  https://quiet-fox-12.trycloudflare.com  |', flush=True)\nprint('ERR failed to connect to edge', flush=True)\n")
voice_bridge.find_cloudflared = lambda: sys.executable
_popen = voice_bridge.subprocess.Popen
voice_bridge.subprocess.Popen = lambda cmd, **k: _popen([sys.executable, str(fake)], **k)
voice_bridge.PORT = 0
_sleep = voice_bridge.time.sleep
def stop(_s):
    raise SystemExit
voice_bridge.time.sleep = stop
th = threading.Thread(target=lambda: (lambda: voice_bridge.run_forever({}, log=lambda *a: None))() , daemon=True)
th.start(); th.join(20)
st = voice_bridge.tunnel_status()
ok(st.get("state") == "restarting" and "failed to connect" in st.get("error", ""), f"a tunnel that drops says why ({st})")
ok("tunnel up at https://quiet-fox-12.trycloudflare.com" in voice_bridge.LOGF.read_text(), "the log file shows the tunnel address")
ok(voice_bridge.state()["public_url"] == "https://quiet-fox-12.trycloudflare.com", "the voice agents were pointed at the tunnel")
voice_bridge.time.sleep = _sleep

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED"))
sys.exit(1 if FAILS else 0)
