"""JARVIS HQ on your home network: serves the JARVIS app to a headset (Meta Quest Browser) or any browser.

Why this exists: WebXR only runs on a secure (HTTPS) page, and the Quest can't reach the Claude desktop
app's local PC link. This server gives the headset the same app and the same engine operations
(crm_pull / crm_push, the exact functions the PC link calls), behind a paired-device token.

  python run.py web            serve on your network at https://<this PC's IP>:8443 (self-signed certificate)
  python run.py web --pair     print a new 6-digit pairing code (the running server accepts it)
  python run.py web --local    only this PC (127.0.0.1); use with USB port forwarding
  python run.py web --revoke   unpair every device

Security:
- Every engine call needs a device token. A device gets one only by entering the pairing code shown on
  this PC (6 digits, 15 minutes, 5 tries). Only a hash of each token is stored.
- Only the app's own sync tools are exposed. The read tool can't write; approvals still go through the
  engine's hash-bound approval checks. No API keys or secrets are ever sent to the browser.
- The headset is only a screen. Agents keep running in `run.py loop` whether or not it's connected.
"""
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db
from .config import ROOT

WEB_DIR = ROOT / "web"
APP_FILE = WEB_DIR / "jarvis.html"
STATE_DIR = ROOT / "data" / "web"
LOG = STATE_DIR / "web.log"
TOOLS = ("crm_pull", "crm_push", "list_counties")
MAX_BODY = 8_000_000
PAIR_TTL = 15 * 60
PAIR_TRIES = 5
_call_lock = threading.Lock()
_pair_lock = threading.Lock()
_tools = {}


def _h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()


def _log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


# ---------------- pairing and device tokens ----------------
def new_pairing_code(con=None):
    """Make a fresh 6-digit code (replaces any earlier one) and return it."""
    con = con or db.connect()
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.kv_set(con, "web_pair", json.dumps({"hash": _h(code), "expires": time.time() + PAIR_TTL, "tries": 0}))
    return code


def _devices(con):
    try:
        return json.loads(db.kv_get(con, "web_devices", "[]") or "[]")
    except Exception:
        return []


def pair(con, code, name=""):
    """Exchange the pairing code for a device token. Returns (token, error)."""
    with _pair_lock:
        try:
            p = json.loads(db.kv_get(con, "web_pair", "{}") or "{}")
        except Exception:
            p = {}
        if not p.get("hash") or time.time() > float(p.get("expires") or 0):
            return None, "No active pairing code. Run Start-HQ-VR (or `python run.py web --pair`) on your PC for a new one."
        if int(p.get("tries") or 0) >= PAIR_TRIES:
            return None, "Too many wrong codes. Get a new code on your PC."
        if not hmac.compare_digest(p["hash"], _h(str(code or "").strip())):
            p["tries"] = int(p.get("tries") or 0) + 1
            db.kv_set(con, "web_pair", json.dumps(p))
            left = PAIR_TRIES - p["tries"]
            return None, f"That code isn't right. {left} tr{'y' if left == 1 else 'ies'} left." if left else "Too many wrong codes. Get a new code on your PC."
        token = secrets.token_urlsafe(32)
        devs = _devices(con)
        devs.append({"id": secrets.token_hex(4), "name": str(name or "Device")[:60], "hash": _h(token),
                     "created": datetime.now().isoformat(timespec="seconds"), "last": None})
        db.kv_set(con, "web_devices", json.dumps(devs))
        db.kv_set(con, "web_pair", "{}")  # single use
        return token, None


def device_for(con, token):
    if not token:
        return None
    th = _h(token)
    for d in _devices(con):
        if hmac.compare_digest(d.get("hash", ""), th):
            return d
    return None


def touch_device(con, dev_id):
    devs = _devices(con)
    for d in devs:
        if d.get("id") == dev_id:
            d["last"] = datetime.now().isoformat(timespec="seconds")
    db.kv_set(con, "web_devices", json.dumps(devs))


def revoke_all(con=None):
    con = con or db.connect()
    n = len(_devices(con))
    db.kv_set(con, "web_devices", "[]")
    db.kv_set(con, "web_pair", "{}")
    return n


# ---------------- network + certificate ----------------
def lan_ips():
    ips = set()
    try:  # the address this PC uses to reach the internet (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
            if a.version == 4 and not a.is_loopback and not a.is_link_local:
                out.append(ip)
        except ValueError:
            pass
    return sorted(out, key=lambda x: (not ipaddress.ip_address(x).is_private, x))


def ensure_cert(ips):
    """Self-signed certificate for this PC's addresses. Re-made when the PC's address changes."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cert, key, meta = STATE_DIR / "cert.pem", STATE_DIR / "key.pem", STATE_DIR / "cert.json"
    want = sorted(set(ips) | {"127.0.0.1"})
    try:
        have = json.loads(meta.read_text())
        if cert.exists() and key.exists() and set(want) <= set(have.get("ips", [])) and have.get("until", 0) > time.time() + 7 * 86400:
            return cert, key
    except Exception:
        pass
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SystemExit("The HTTPS certificate needs the 'cryptography' package. Run Setup.bat again, or: .venv\\Scripts\\pip install cryptography")
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS HQ (this PC, self-signed)")])
    host = socket.gethostname()
    san = [x509.DNSName("localhost")] + ([x509.DNSName(host)] if host and all(c.isalnum() or c in "-." for c in host) else [])
    san += [x509.IPAddress(ipaddress.ip_address(i)) for i in want]
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=397)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(until)
         .add_extension(x509.SubjectAlternativeName(san), critical=False)
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    meta.write_text(json.dumps({"ips": want, "until": until.timestamp()}))
    return cert, key


# ---------------- engine tools (the same functions the PC link calls) ----------------
def _tool(name):
    if not _tools:
        import mcp_server as m  # noqa: WPS433  (imports the tool functions; doesn't start the MCP server)
        for n in TOOLS:
            _tools[n] = getattr(m, n)
    return _tools[name]


def call_tool(name, args):
    if name not in TOOLS:
        raise PermissionError(f"'{name}' isn't available over the network.")
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    fn = _tool(name)
    with _call_lock:
        return fn(**args)


def app_html():
    if not APP_FILE.exists():
        return ("<!doctype html><meta charset=utf-8><title>JARVIS HQ</title><body style='font:16px system-ui;background:#0b0f14;color:#e6edf3;padding:24px'>"
                "<h2>JARVIS app file missing</h2><p>Put <code>jarvis.html</code> in the <code>web</code> folder of JARVIS Engine.</p>")
    html = APP_FILE.read_text(encoding="utf-8")
    inject = "<script>window.__JARVIS_WEB__={v:1};</script>"
    i = html.lower().find("<head>")
    return html[: i + 6] + inject + html[i + 6:] if i >= 0 else inject + html


class Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS"
    sys_version = ""

    def log_message(self, fmt, *a):  # quiet console; errors go to data/web/web.log
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "charset" not in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Permissions-Policy", "xr-spatial-tracking=(self)")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _device(self, con):
        auth = self.headers.get("Authorization") or ""
        return device_for(con, auth[7:].strip() if auth.lower().startswith("bearer ") else "")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/hq"):
            return self._send(200, app_html(), "text/html")
        if path == "/api/health":
            con = db.connect()
            return self._send(200, {"ok": True, "app": "JARVIS Engine", "paired": bool(self._device(con))})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
        except Exception as e:
            return self._send(400, {"error": f"bad request: {e}"})
        con = db.connect()
        if path == "/api/pair":
            token, err = pair(con, body.get("code"), body.get("name"))
            _log(f"pair {'ok' if token else 'refused'} from {self.client_address[0]}")
            return self._send(200, {"token": token}) if token else self._send(403, {"error": err})
        if path == "/api/tool":
            dev = self._device(con)
            if not dev:
                return self._send(401, {"error": "This device isn't paired.", "code": "not_paired"})
            name, args = body.get("name"), body.get("args") or {}
            op = ((args.get("hunter") or {}).get("op") if isinstance(args, dict) else "") or ""
            try:
                out = call_tool(name, args)
            except PermissionError as e:
                _log(f"refused {name} device={dev['id']}")
                return self._send(403, {"error": str(e), "code": "not_allowed"})
            except (TypeError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                _log(f"error {name} {op} device={dev['id']}: {traceback.format_exc()}")
                return self._send(500, {"error": f"{e.__class__.__name__}: {e}"})
            if name != "crm_pull" or op:
                _log(f"{name} {op} device={dev['id']}")
            if not dev.get("last") or dev["last"][:16] != datetime.now().isoformat()[:16]:
                touch_device(con, dev["id"])
            return self._send(200, {"payload": out})
        return self._send(404, {"error": "not found"})


def make_server(port=8443, local=False, use_tls=True):
    host = "127.0.0.1" if local else "0.0.0.0"
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.handle_error = lambda request, addr: _log(f"connection error from {addr[0]}: {traceback.format_exc().strip().splitlines()[-1]}")
    if use_tls:
        cert, key = ensure_cert([] if local else lan_ips())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


def serve(port=8443, local=False, use_tls=True):
    srv = make_server(port, local, use_tls)
    code = new_pairing_code()
    scheme = "https" if use_tls else "http"
    urls = [f"{scheme}://localhost:{port}"] if local else [f"{scheme}://{ip}:{port}" for ip in lan_ips()] or [f"{scheme}://<this PC's IP>:{port}"]
    print("\n  MOLABUCKS CAPITAL HQ · web and VR\n")
    print("  On your Quest, open Meta Quest Browser and go to:")
    for u in urls:
        print(f"      {u}")
    if use_tls:
        print("  The certificate is made by this PC, so the browser warns once: choose Advanced, then Proceed.")
    print(f"\n  Pairing code (15 minutes): {code[:3]} {code[3:]}")
    print("  Keep this window open while you use HQ on the headset. The agents run in the JARVIS Engine window either way.\n")
    _log(f"serving {', '.join(urls)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
