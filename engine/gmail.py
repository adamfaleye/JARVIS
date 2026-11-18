"""Gmail for JARVIS Engine: send approved AI Sidekicks emails and read replies, without the Claude app open.

One-time setup on the PC (see README "Engine email"): put your Google OAuth desktop client file at data/google_client.json,
then run `python run.py gmail-auth`, sign in with your Google account in the browser that opens, and allow access.
The refresh token is saved to data/gmail_token.json on this PC only. Nothing here stores or asks for your password.
Standard library only (no extra installs)."""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from email.utils import make_msgid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import db

SCOPES = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
LOGO = Path(__file__).resolve().parent / "assets" / "aisidekicks-logo-email.png"  # the AI Sidekicks logo (Masked hero)
LOGO_CIDS = ("aisidekicks-logo", "demigod-logo")  # emails approved before the rename keep the older id
_cache = {"access": None, "exp": 0}


def _data():
    return db.DB_PATH.parent


def client_path():
    return _data() / "google_client.json"


def token_path():
    return _data() / "gmail_token.json"


def configured():
    return client_path().exists() and token_path().exists()


def _client():
    raw = json.loads(client_path().read_text(encoding="utf-8"))
    c = raw.get("installed") or raw.get("web") or raw
    if not c.get("client_id") or not c.get("client_secret"):
        raise RuntimeError("data/google_client.json isn't a Google OAuth client file (Desktop app).")
    return c


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Google sign-in error {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")


def authorize(open_browser=True, log=print):
    """Browser sign-in (PKCE, loopback). You sign in to Google yourself; this only receives the one-time code."""
    c = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>JARVIS Engine is connected to Gmail.</h2><p>You can close this tab.</p>".encode() if "code" in got
                             else "<h2>Sign-in didn't finish.</h2><p>Go back to the JARVIS window.</p>".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
                                                   "access_type": "offline", "prompt": "consent", "state": state,
                                                   "code_challenge": challenge, "code_challenge_method": "S256"})
    log("Opening Google sign-in in your browser. If it doesn't open, paste this link into it:\n" + url)
    if open_browser:
        webbrowser.open(url)
    th = threading.Thread(target=lambda: [srv.handle_request() for _ in range(5) if "code" not in got and "error" not in got], daemon=True)
    th.start()
    th.join(timeout=300)
    srv.server_close()
    if "code" not in got:
        raise RuntimeError("Google sign-in didn't complete" + (f": {got['error']}" if got.get("error") else " (timed out after 5 minutes)."))
    tok = _post(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c["client_secret"], "code": got["code"], "code_verifier": verifier,
                            "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not tok.get("refresh_token"):
        raise RuntimeError("Google didn't return a refresh token. Remove JARVIS's access at myaccount.google.com/permissions and run gmail-auth again.")
    token_path().write_text(json.dumps({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", ""), "at": int(time.time())}), encoding="utf-8")
    try:
        os.chmod(token_path(), 0o600)
    except OSError:
        pass
    _cache.update(access=tok.get("access_token"), exp=time.time() + int(tok.get("expires_in", 3000)) - 60)
    return profile()


def _access():
    if _cache["access"] and time.time() < _cache["exp"]:
        return _cache["access"]
    c, t = _client(), json.loads(token_path().read_text(encoding="utf-8"))
    try:
        tok = _post(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    except RuntimeError as e:
        if "invalid_grant" in str(e):  # Google ends sign-ins after 7 days while the app is in Testing, or after a password change
            raise RuntimeError("Gmail sign-in expired. Double-click Connect-Gmail.bat in the JARVIS folder to sign in again, then restart JARVIS.") from None
        raise
    _cache.update(access=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3000)) - 60)
    return _cache["access"]


def _api(method, path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + _access(), "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gmail error {e.code}: {e.read()[:300].decode('utf-8', 'ignore')}")


def profile():
    return _api("GET", "/profile")


def build_message(to, subject, text, html=None, sender=None, logo=True):
    m = EmailMessage()
    m["To"] = to
    m["Subject"] = subject
    if sender:
        m["From"] = sender
    m["Message-ID"] = make_msgid(domain="aisidekicks.local")
    m.set_content(text)
    if html:
        cid = next((c for c in LOGO_CIDS if f"cid:{c}" in html), None)
        if logo and cid and LOGO.exists():
            m.add_alternative(html, subtype="html")
            m.get_payload()[1].add_related(LOGO.read_bytes(), maintype="image", subtype="png", cid=f"<{cid}>", filename="ai-sidekicks.png")
        else:
            for c in LOGO_CIDS:
                html = html.replace(f'<img src="cid:{c}"', '<img data-x="none"')
            m.add_alternative(html, subtype="html")
    return m


def send(to, subject, text, html=None, thread_id=None):
    raw = base64.urlsafe_b64encode(build_message(to, subject, text, html).as_bytes()).decode()
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    r = _api("POST", "/messages/send", body)
    if not r.get("id"):
        raise RuntimeError("Gmail didn't return a message id.")
    return {"id": r["id"], "threadId": r.get("threadId", "")}


def _text_of(payload):
    if not payload:
        return ""
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "ignore")
    for p in payload.get("parts") or []:
        t = _text_of(p)
        if t:
            return t
    return ""


def thread(thread_id):
    """Messages in a thread: [{id, from, internalDate, text}]"""
    r = _api("GET", f"/threads/{urllib.parse.quote(thread_id)}?format=full")
    out = []
    for m in r.get("messages") or []:
        hs = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}
        out.append({"id": m.get("id"), "from": hs.get("from", ""), "internalDate": m.get("internalDate"), "text": _text_of(m.get("payload"))[:6000],
                    "labels": m.get("labelIds") or []})
    return out
