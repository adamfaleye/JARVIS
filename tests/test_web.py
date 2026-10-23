"""HQ web/VR server tests. Run: python -m tests.test_web  (throwaway database, local port, self-signed cert)."""
import json, os, ssl, sys, tempfile, threading, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import db, config, web

tmp = Path(tempfile.mkdtemp())
db.DB_PATH = tmp / "test.db"
config.ROOT = tmp
web.STATE_DIR = tmp / "data" / "web"; web.LOG = web.STATE_DIR / "web.log"
web.WEB_DIR = tmp / "web"; web.APP_FILE = web.WEB_DIR / "jarvis.html"
web.WEB_DIR.mkdir(parents=True)
web.APP_FILE.write_text("<!doctype html><html><head><title>JARVIS</title></head><body>app</body></html>", encoding="utf-8")
FAILS = []
def ok(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: FAILS.append(m)

srv = web.make_server(0, local=True, use_tls=True)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
BASE = f"https://127.0.0.1:{port}"
def req(path, body=None, token=None):
    r = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                               headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=10) as res:
            raw = res.read().decode(); return res.status, (json.loads(raw) if res.headers.get_content_type() == "application/json" else raw), res.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), e.headers

s, html, h = req("/")
ok(s == 200 and "window.__JARVIS_WEB__" in html and html.index("__JARVIS_WEB__") < html.index("<title>"), "serves the app over HTTPS with the web flag in <head>")
ok("xr-spatial-tracking=(self)" in (h.get("Permissions-Policy") or "") and h.get("Cache-Control") == "no-store", "XR permission policy and no-store headers")
ok(not (web.STATE_DIR / "key.pem").read_text().count("ENCRYPTED") and (web.STATE_DIR / "cert.pem").exists(), "self-signed certificate made on first start")

s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {"since": ""}})
ok(s == 401 and b.get("code") == "not_paired", "engine calls without a device token are refused")
s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {}}, token="made-up")
ok(s == 401, "an unknown token is refused")

s, b, _ = req("/api/pair", {"code": "123456"})
ok(s == 403 and "No active pairing code" in b["error"], "pairing refused when no code is active")
code = web.new_pairing_code(db.connect())
wrong = "000000" if code != "000000" else "111111"
s, b, _ = req("/api/pair", {"code": wrong})
ok(s == 403 and "4 tries left" in b["error"], "a wrong code counts down the tries")
s, b, _ = req("/api/pair", {"code": code[:3] + " " + code[3:]})
ok(s == 403, "code must match exactly")
s, b, _ = req("/api/pair", {"code": code, "name": "Quest 2"})
tok = b.get("token")
ok(s == 200 and tok and len(tok) > 30, "the right code returns a device token")
devs = json.loads(db.kv_get(db.connect(), "web_devices"))
ok(len(devs) == 1 and devs[0]["name"] == "Quest 2" and tok not in json.dumps(devs), "only a hash of the token is stored")
s, b, _ = req("/api/pair", {"code": code})
ok(s == 403, "a pairing code works once")

code2 = web.new_pairing_code(db.connect())
for i in range(5):
    req("/api/pair", {"code": "999999" if code2 != "999999" else "888888"})
s, b, _ = req("/api/pair", {"code": code2})
ok(s == 403 and "Too many" in b["error"], "five wrong tries lock the code, even the right one")

s, b, _ = req("/api/health", token=tok)
ok(s == 200 and b["paired"] is True, "health reports a paired device")
s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {"since": ""}}, token=tok)
ok(s == 200 and "now" in b["payload"] and isinstance(b["payload"]["items"], list), "crm_pull works for the paired device (same function as the PC link)")
item = {"kind": "deals", "id": "d_web1", "ts": "2026-10-20T12:00:00.000Z", "data": {"id": "d_web1", "address": "1 Test St", "stage": "New Lead", "_ts": "2026-10-20T12:00:00.000Z"}}
s, b, _ = req("/api/tool", {"name": "crm_push", "args": {"items": [item], "deletes": []}}, token=tok)
ok(s == 200 and b["payload"].get("applied") == 1, "crm_push saves a record from the headset")
s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {"since": "", "hunter": {"op": "hq.pause"}}}, token=tok)
ok(s == 200 and "isn't allowed" in json.dumps(b["payload"].get("hunter")), "the read channel still refuses writes")
s, b, _ = req("/api/tool", {"name": "run_scrape", "args": {}}, token=tok)
ok(s == 403 and b.get("code") == "not_allowed", "tools outside the app's sync set aren't exposed")
s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {"bogus": 1}}, token=tok)
ok(s == 400, "bad arguments are a 400, not a crash")
ok("crm_push" in web.LOG.read_text() and tok not in web.LOG.read_text(), "writes are logged without the token")

ok(web.revoke_all(db.connect()) == 1, "revoke unpairs every device")
s, b, _ = req("/api/tool", {"name": "crm_pull", "args": {}}, token=tok)
ok(s == 401, "a revoked token stops working")
srv.shutdown()
print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
