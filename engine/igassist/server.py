"""The client dashboard (Mola's, for AI Sidekicks), its JSON API and the Instagram webhook.

  python run.py assistant serve [--port 8600] [--host 127.0.0.1]

Meta can only deliver webhooks to a public HTTPS address with a real certificate, so in production this server sits
behind an HTTPS tunnel or host (see AISIDEKICKS.md). Locally it binds to 127.0.0.1 by default.

Security: session cookie (HttpOnly, SameSite=Strict, Secure behind HTTPS), CSRF header on every write, role checks
in the workflow itself, per-response CSP nonce, no secrets ever sent to the browser.
"""
import hashlib
import json
import re
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analytics, auth, kb, meta, outbox, service, store, workflow, brain

STATIC = Path(__file__).resolve().parent / "dashboard.html"
MAX_BODY = 2_000_000
COOKIE = "iga_session"


class App:
    def __init__(self, tenant, transport=None):
        self.tenant = tenant
        self.con = store.connect(tenant)
        kb.seed(self.con)
        self.lock = threading.RLock()
        self.transport = transport or meta.GraphTransport(tenant)
        self.stop = threading.Event()
        self.last_tick = None

    def worker(self, every=3):
        while not self.stop.is_set():
            try:
                with self.lock:
                    service.tick(self.con, self.tenant, self.transport)
                self.last_tick = time.time()
            except Exception:  # noqa: BLE001
                store.health(self.con, "worker", False, traceback.format_exc()[-300:])
            self.stop.wait(every)


def _err(code, msg):
    return code, {"error": msg}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "JARVIS"
        sys_version = ""

        def log_message(self, *a):
            pass

        # ---------------------------------------------------------- plumbing
        def _https(self):
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https" or getattr(self.server, "tls", False)

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if self._https():
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _raw(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("request too large")
            return self.rfile.read(n) if n else b""

        def _token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return ""

        def _user(self):
            with app.lock:
                return auth.session(app.con, self._token())

        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

        # ---------------------------------------------------------- GET
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            path, qs = u.path, dict(urllib.parse.parse_qsl(u.query))
            if path in ("/", "/index.html"):
                nonce = secrets.token_urlsafe(16)
                html = STATIC.read_text(encoding="utf-8").replace("{{NONCE}}", nonce)
                return self._send(200, html, "text/html", {
                    "Content-Security-Policy": f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                                               "img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
            if path == "/webhooks/instagram":
                ch = meta.verify_challenge(app.tenant, qs)
                with app.lock:
                    store.health(app.con, "webhook_verify", ch is not None, "verification request")
                return self._send(200, ch, "text/plain") if ch is not None else self._send(403, "forbidden", "text/plain")
            if path == "/setup":
                nonce = secrets.token_urlsafe(16)
                with app.lock:
                    page = setup_html(app.con, nonce)
                return self._send(200, page, "text/html", {
                    "Content-Security-Policy": f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/brand":
                with app.lock:
                    pr = kb.profile(app.con)
                return self._send(200, {"brand": pr["brand"], "subtitle": pr.get("subtitle") or ""})
            if path == "/api/health":
                return self._send(200, {"ok": True, "worker_seconds_ago": round(time.time() - app.last_tick) if app.last_tick else None})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            user = self._user()
            if not user:
                return self._send(401, {"error": "Sign in first.", "code": "signin"})
            try:
                with app.lock:
                    code, body = self.route_get(path, qs, user)
            except (ValueError, workflow.WorkflowError) as e:
                code, body = 400, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                store.audit(app.con, "server", "error", path, {"trace": traceback.format_exc()[-1500:]})
                code, body = 500, {"error": f"Something went wrong ({e.__class__.__name__}). It's logged."}
            return self._send(code, body)

        def route_get(self, path, qs, user):
            con = app.con
            inc = qs.get("test", "1") == "1"
            if path == "/api/me":
                return 200, {"user": {k: v for k, v in user.items() if k != "csrf"}, "csrf": user["csrf"], "tenant": app.tenant,
                             "settings": settings_view(con), "labels": workflow.labels(con), "profile": kb.profile(con),
                             "has_operator": bool(con.execute("SELECT 1 FROM users WHERE role='operator'").fetchone())}
            if path == "/api/queue":
                return 200, workflow.queue(con, inc)
            m = re.fullmatch(r"/api/appointments/(\d+)", path)
            if m:
                aid = int(m.group(1))
                if qs.get("review") == "1":
                    workflow.mark_reviewing(con, aid, user)
                d = workflow.detail(con, aid)
                d["customer"] = dict(con.execute("SELECT * FROM customers WHERE id=?", (d["customer_id"],)).fetchone() or {})
                d["messages"] = _messages(con, d["conversation_id"], 30)
                d["confirmation_preview"] = brain.confirmation_text(con, d)
                return 200, d
            if path == "/api/appointments":
                st = qs.get("status")
                rows = con.execute("SELECT id FROM appointments " + ("WHERE status=? " if st else "") + "ORDER BY id DESC LIMIT 200",
                                   (st,) if st else ()).fetchall()
                return 200, {"items": [workflow.get(con, r["id"]) for r in rows]}
            if path == "/api/conversations":
                rows = con.execute("SELECT c.*, cu.username, cu.name, cu.igsid, (SELECT text FROM messages WHERE conversation_id=c.id ORDER BY id DESC LIMIT 1) last_text, "
                                   "(SELECT COUNT(*) FROM messages WHERE conversation_id=c.id AND status IN ('draft','blocked')) drafts "
                                   "FROM conversations c JOIN customers cu ON cu.id=c.customer_id " + ("" if inc else "WHERE c.is_test=0 ") +
                                   "ORDER BY c.updated_at DESC LIMIT 200").fetchall()
                return 200, {"items": [dict(r) for r in rows]}
            m = re.fullmatch(r"/api/conversations/(\d+)", path)
            if m:
                cid = int(m.group(1))
                c = con.execute("SELECT c.*, cu.username, cu.name, cu.igsid, cu.id customer_id FROM conversations c JOIN customers cu ON cu.id=c.customer_id WHERE c.id=?", (cid,)).fetchone()
                if not c:
                    return 404, {"error": "Conversation not found."}
                con.execute("UPDATE conversations SET unread=0 WHERE id=?", (cid,))
                con.commit()
                appts = [workflow.get(con, r["id"]) for r in con.execute("SELECT id FROM appointments WHERE conversation_id=? ORDER BY id DESC", (cid,))]
                return 200, {"conversation": dict(c), "messages": _messages(con, cid, 200), "appointments": appts,
                             "window_open": outbox._window_open(c)}
            if path == "/api/customers":
                rows = con.execute("SELECT cu.*, (SELECT COUNT(*) FROM appointments a WHERE a.customer_id=cu.id) appts, "
                                   "(SELECT MAX(status) FROM appointments a WHERE a.customer_id=cu.id) any_status "
                                   "FROM customers cu " + ("" if inc else "WHERE cu.is_test=0 ") + "ORDER BY cu.updated_at DESC LIMIT 500").fetchall()
                return 200, {"items": [dict(r) for r in rows]}
            m = re.fullmatch(r"/api/customers/(\d+)(/export)?", path)
            if m:
                d = service.export_customer(con, int(m.group(1)))
                if not d:
                    return 404, {"error": "Customer not found."}
                if m.group(2):
                    store.audit(con, user["username"], "customer.export", m.group(1))
                    con.commit()
                return 200, d
            if path == "/api/kb":
                return 200, {"items": kb.all_items(con), "missing": kb.missing_facts(con)}
            if path == "/api/analytics":
                return 200, analytics.report(con, int(qs.get("days", "30")), qs.get("test") == "1")
            if path == "/api/settings":
                h = {r["name"]: dict(r) for r in con.execute("SELECT * FROM integration_health")}
                return 200, {"settings": settings_view(con), "meta": meta.configured(app.tenant), "health": h,
                             "worker_seconds_ago": round(time.time() - app.last_tick) if app.last_tick else None,
                             "users": [dict(r) for r in con.execute("SELECT username,name,role,disabled FROM users")] if user["role"] == "operator" else None,
                             "audit": [dict(r) for r in con.execute("SELECT at,actor,action,target FROM audit ORDER BY id DESC LIMIT 60")]}
            return 404, {"error": "not found"}

        # ---------------------------------------------------------- POST
        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                raw = self._raw()
            except ValueError as e:
                return self._send(413, {"error": str(e)})
            if path == "/webhooks/instagram":
                ok = meta.signature_ok(app.tenant, raw, self.headers.get("X-Hub-Signature-256", ""))
                with app.lock:
                    if ok:
                        meta.store_event(app.con, raw, True)
                        store.health(app.con, "webhook_receive", True, "event received")
                    else:
                        store.audit(app.con, "webhook", "signature_rejected", "", {"ip": self._ip(), "bytes": len(raw)})
                        store.health(app.con, "webhook_receive", False, "bad or missing signature")
                        app.con.commit()
                return self._send(200, {"ok": True}) if ok else self._send(403, {"error": "bad signature"})
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad request"})
            if path == "/api/setup":
                with app.lock:
                    try:
                        first_run_setup(app.con, body)
                    except ValueError as e:
                        time.sleep(0.4)
                        return self._send(400, {"error": str(e)})
                return self._send(200, {"ok": True})
            if path == "/api/login":
                try:
                    with app.lock:
                        token, csrf, user = auth.login(app.con, body.get("username"), body.get("password"), self._ip())
                except ValueError as e:
                    time.sleep(0.4)
                    return self._send(401, {"error": str(e)})
                flags = "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if self._https() else "")
                return self._send(200, {"user": user, "csrf": csrf}, extra={"Set-Cookie": f"{COOKIE}={token}; {flags}; Max-Age={auth.SESSION_HOURS * 3600}"})
            user = self._user()
            if not user:
                return self._send(401, {"error": "Sign in first.", "code": "signin"})
            if not secrets.compare_digest(self.headers.get("X-CSRF", ""), user["csrf"]):
                return self._send(403, {"error": "Session check failed. Reload the page."})
            if path == "/api/logout":
                with app.lock:
                    auth.logout(app.con, self._token())
                return self._send(200, {"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            try:
                with app.lock:
                    code, out = self.route_post(path, body, user)
            except workflow.Forbidden as e:
                code, out = 403, {"error": str(e)}
            except workflow.Conflict as e:
                code, out = 409, {"error": str(e)}
            except (ValueError, workflow.WorkflowError) as e:
                code, out = 400, {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                store.audit(app.con, "server", "error", path, {"trace": traceback.format_exc()[-1500:]})
                app.con.commit()
                code, out = 500, {"error": f"Something went wrong ({e.__class__.__name__}). It's logged."}
            return self._send(code, out)

        def route_post(self, path, b, user):
            con = app.con
            m = re.fullmatch(r"/api/appointments/(\d+)/([a-z_]+)", path)
            if m:
                aid, act = int(m.group(1)), m.group(2)
                v = b.get("version")
                if act == "edit":
                    return 200, workflow.edit(con, aid, user, b.get("fields") or {}, v)
                if act == "verify_availability":
                    return 200, workflow.verify_availability(con, aid, user, b.get("date"), b.get("time"), v, b.get("note") or "")
                if act == "approve":
                    return 200, workflow.approve(con, aid, user, v)
                if act == "mark_entered":
                    return 200, workflow.mark_entered(con, aid, user, b.get("attest") is True, b.get("calendar_ref") or "", v)
                if act == "mark_confirmed":
                    a = workflow.mark_confirmed(con, aid, user, b.get("attest") is True, v)
                    if b.get("send_confirmation"):
                        txt = (b.get("message") or brain.confirmation_text(con, a)).strip()
                        mid = outbox.enqueue(con, a["conversation_id"], txt, author="owner", kind="confirmation", intent="confirmation", appointment_id=aid)
                        a["confirmation_queued"] = mid
                    return 200, a
                if act == "request_info":
                    a = workflow.request_info(con, aid, user, b.get("question"), v)
                    if b.get("send", True):
                        outbox.enqueue(con, a["conversation_id"], b.get("question"), author="owner", intent="request_info", appointment_id=aid)
                    return 200, a
                if act == "decline":
                    a = workflow.decline(con, aid, user, b.get("reason"), v)
                    if (b.get("message") or "").strip():
                        outbox.send_manual(con, a["conversation_id"], b["message"], user)
                    return 200, a
                if act == "cancel":
                    a = workflow.cancel(con, aid, user, b.get("reason"), v)
                    if (b.get("message") or "").strip():
                        outbox.send_manual(con, a["conversation_id"], b["message"], user)
                    return 200, a
                if act == "complete":
                    return 200, workflow.complete(con, aid, user, v)
                if act == "return_to_pending":
                    return 200, workflow.return_to_pending(con, aid, user, v)
                if act == "note":
                    workflow.add_note(con, aid, user["username"], b.get("text"))
                    return 200, workflow.detail(con, aid)
                return 404, {"error": "Unknown action."}
            m = re.fullmatch(r"/api/conversations/(\d+)/([a-z_]+)", path)
            if m:
                cid, act = int(m.group(1)), m.group(2)
                if not con.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                    return 404, {"error": "Conversation not found."}
                if act == "takeover":
                    con.execute("UPDATE conversations SET mode='human', updated_at=? WHERE id=?", (store.now(), cid))
                    con.execute("UPDATE messages SET status='discarded', hold_reason='The owner took over' WHERE conversation_id=? AND author='ai' "
                                "AND status IN ('draft','queued') AND kind!='holding'", (cid,))
                    store.audit(con, user["username"], "conversation.takeover", cid)
                elif act == "release":
                    con.execute("UPDATE conversations SET mode='ai', status='open', escalation_reason=NULL, updated_at=? WHERE id=?", (store.now(), cid))
                    store.audit(con, user["username"], "conversation.release", cid)
                elif act == "close":
                    con.execute("UPDATE conversations SET status='closed', updated_at=? WHERE id=?", (store.now(), cid))
                    store.audit(con, user["username"], "conversation.close", cid)
                elif act == "send":
                    outbox.send_manual(con, cid, b.get("text"), user)
                    if b.get("takeover", True):
                        con.execute("UPDATE conversations SET mode='human', updated_at=? WHERE id=?", (store.now(), cid))
                elif act == "redraft":
                    last = con.execute("SELECT id FROM messages WHERE conversation_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                    if not last:
                        raise ValueError("No customer message to reply to.")
                    c = con.execute("SELECT mode FROM conversations WHERE id=?", (cid,)).fetchone()
                    if c["mode"] == "human":
                        raise ValueError("You have this conversation. Hand it back to the AI first.")
                    brain.handle(con, cid, last["id"])
                else:
                    return 404, {"error": "Unknown action."}
                con.commit()
                return 200, {"ok": True}
            m = re.fullmatch(r"/api/messages/(\d+)/(approve|discard)", path)
            if m:
                mid = int(m.group(1))
                if m.group(2) == "approve":
                    return 200, {"edited": outbox.approve_draft(con, mid, user, b.get("text"))}
                return 200, {"ok": outbox.discard(con, mid, user)}
            m = re.fullmatch(r"/api/customers/(\d+)(/delete)?", path)
            if m:
                cid = int(m.group(1))
                if m.group(2):
                    if b.get("confirm") != "DELETE":
                        raise ValueError('Type DELETE to confirm.')
                    return 200, {"ok": service.delete_customer(con, cid, user["username"])}
                sets = {k: (str(b[k])[:500] if b[k] else None) for k in ("name", "notes", "contact") if k in b}
                if sets:
                    con.execute(f"UPDATE customers SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), store.now(), cid))
                    store.audit(con, user["username"], "customer.update", cid, {"fields": list(sets)})
                    con.commit()
                return 200, {"ok": True}
            if path == "/api/kb":
                return 200, kb.save(con, b.get("kind"), b.get("key"), b.get("data") or {}, user["username"], b.get("version"))
            if path == "/api/kb/delete":
                return 200, {"ok": kb.delete(con, b.get("kind"), b.get("key"), user["username"])}
            if path == "/api/settings":
                return 200, {"settings": update_settings(con, app.tenant, user, b)}
            if path == "/api/simulate":
                eid = service.simulate_inbound(con, b.get("sender") or "tester", b.get("text"))
                service.tick(con, app.tenant, app.transport)
                return 200, {"event": eid}
            return 404, {"error": "not found"}

    return H


SETUP_TTL_MIN = 60


def new_setup_code(con):
    """First run only (no logins yet): a one-time code that lets the browser create the two logins.
    Only a hash is stored; it expires after an hour and stops working once any login exists."""
    code = secrets.token_urlsafe(18)
    store.put(con, "setup_code_hash", hashlib.sha256(code.encode()).hexdigest(), "system")
    store.put(con, "setup_code_expires", str(time.time() + SETUP_TTL_MIN * 60), "system")
    return code


def setup_plan(con):
    """Logins the first-run page creates: the owner (approves bookings) and, if that isn't Mola, Mola as operator."""
    p = kb.profile(con)
    owner_user = (p.get("owner_username") or p["owner"]).strip().lower().replace(" ", "")
    plan = [{"username": owner_user, "name": p["owner"], "role": "owner", "field": "owner_password"}]
    if owner_user != "mola":
        plan.insert(0, {"username": "mola", "name": "Mola", "role": "operator", "field": "mola_password"})
    return plan


def first_run_setup(con, b):
    if con.execute("SELECT 1 FROM users").fetchone():
        raise ValueError("Logins already exist. Sign in on the main page.")
    h, exp = store.get(con, "setup_code_hash") or "", float(store.get(con, "setup_code_expires") or 0)
    if not h or time.time() > exp or not secrets.compare_digest(hashlib.sha256(str(b.get("code") or "").encode()).hexdigest(), h):
        raise ValueError("This setup link has expired or isn't valid. Close this tab and run the start script again.")
    plan = setup_plan(con)
    pw = {x["field"]: str(b.get(x["field"]) or "") for x in plan}
    if any(len(v) < 10 for v in pw.values()):
        raise ValueError("Passwords need at least 10 characters.")
    if len(set(pw.values())) < len(pw):
        raise ValueError("Use a different password for each login.")
    for x in plan:
        auth.add_user(con, x["username"], x["name"], x["role"], pw[x["field"]], actor="setup")
    store.put(con, "setup_code_hash", "", "system")
    store.put(con, "setup_code_expires", "0", "system")


SETUP_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{{BRAND}} · first-time setup</title><style>body{font:15px system-ui,Segoe UI,sans-serif;background:#faf7f4;color:#1f1a17;margin:0}
form{max-width:420px;margin:8vh auto;background:#fff;border:1px solid #e8e0d8;border-radius:16px;padding:26px}h1{font-size:20px;margin:0 0 4px}
p{color:#6f655e;font-size:14px}label{display:block;font-size:13px;color:#6f655e;margin:14px 0 4px}input{width:100%;box-sizing:border-box;font:inherit;padding:9px 11px;border:1px solid #e8e0d8;border-radius:9px}
button{margin-top:18px;width:100%;padding:10px;border:0;border-radius:9px;background:#8e3b5e;color:#fff;font:inherit;cursor:pointer}#msg{margin-top:12px;font-size:14px;min-height:20px}</style></head>
<body><form id=f><h1>Create the logins</h1><p>One time only for {{BRAND}}. At least 10 characters each.</p>{{FIELDS}}
<button type=submit>Create logins</button><div id=msg></div></form>
<script nonce="{{NONCE}}">const code=location.hash.slice(1);const m=document.getElementById('msg');
if(!code){m.textContent='Open this page from the link the start script opens.';}
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();m.style.color='';m.textContent='Saving…';
const body={code};document.querySelectorAll('input[data-f]').forEach(i=>body[i.dataset.f]=i.value);
const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const j=await r.json().catch(()=>({}));if(r.ok){history.replaceState(null,'','/setup');m.style.color='#2f7d4f';m.textContent='Done. Taking you to sign in…';setTimeout(()=>location.href='/',1500);}
else{m.style.color='#b3261e';m.textContent=j.error||'Something went wrong.';}});</script></body></html>"""


def setup_html(con, nonce):
    import html as _h
    parts = []
    for x in setup_plan(con):
        role = " — approves bookings" if x["role"] == "owner" else " — AI Sidekicks operator"
        parts.append(f"<label>{_h.escape(x['name'])}&#39;s password (username <b>{_h.escape(x['username'])}</b>{role})</label>"
                     f"<input data-f=\"{x['field']}\" type=password autocomplete=new-password required minlength=10>")
    fields = "".join(parts)
    return (SETUP_HTML.replace("{{NONCE}}", nonce).replace("{{FIELDS}}", fields)
            .replace("{{BRAND}}", _h.escape(kb.profile(con)["brand"])))


def _messages(con, cid, limit):
    rows = con.execute("SELECT id,direction,author,text,attachments,status,hold_reason,error,intent,created_at,sent_at,kind,ai_original "
                       "FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()
    return [dict(r) | {"attachments": store.j(r["attachments"], [])} for r in rows][::-1]


SETTINGS_EDIT = {
    "automation_paused": ("owner", "operator"), "reply_mode": ("owner",), "takeover_on_echo": ("owner", "operator"),
    "intro_line": ("owner", "operator"), "voice_person": ("owner",), "minutes_per_reply": ("owner", "operator"),
    "retention_days": ("owner", "operator"), "live_send": ("operator",), "pilot_approved": ("owner",), "baseline": ("owner", "operator"),
}


def settings_view(con):
    return {k: store.get(con, k) for k in list(store.DEFAULT_SETTINGS) + ["baseline"]}


def update_settings(con, tenant, user, b):
    for k, v in b.items():
        if k not in SETTINGS_EDIT:
            raise ValueError(f"'{k}' can't be changed here.")
        roles = SETTINGS_EDIT[k]
        if k == "live_send" and not con.execute("SELECT 1 FROM users WHERE role='operator'").fetchone():
            roles = ("owner", "operator")  # the owner is also the operator (e.g. AI Sidekicks' own account)
        if user["role"] not in roles:
            raise workflow.Forbidden("Only " + " or ".join(kb.profile(con)["owner"] if r == "owner" else "the AI Sidekicks operator" for r in SETTINGS_EDIT[k]) + " can change that.")
        if k == "reply_mode" and v not in ("review", "auto"):
            raise ValueError("Reply mode is review or auto.")
        if k == "voice_person" and v not in ("assistant", "first_person"):
            raise ValueError("Voice is assistant or first_person.")
        if k in ("minutes_per_reply",):
            v = str(float(v))
        if k == "retention_days" and str(v).strip():
            if not 30 <= int(v) <= 3650:
                raise ValueError("Retention is 30-3650 days (or empty to keep).")
        if k == "pilot_approved":
            v = store.now() if v else ""
        if k == "live_send" and str(v) in ("1", "true", "True"):
            if not store.get(con, "pilot_approved"):
                raise ValueError(kb.fmt(con, "{owner} has to approve the pilot (Settings) before live sending can be turned on."))
            if not all(meta.configured(tenant).values()):
                raise ValueError("Instagram isn't fully connected (.env is missing values).")
            v = "1"
        if k == "baseline":
            v = json.dumps({kk: (str(vv)[:200] if vv not in (None, "") else None) for kk, vv in (v or {}).items()
                            if kk in ("dms_per_week", "minutes_per_day_on_dms", "avg_response_time", "booking_requests_per_week",
                                      "confirmed_from_instagram_per_week", "measured_from", "measured_to", "notes")})
        if k in ("automation_paused", "takeover_on_echo", "intro_line", "live_send"):
            v = "1" if str(v) in ("1", "true", "True") else "0"
        store.put(con, k, v, user["username"])
    return settings_view(con)


def make_server(tenant, host="127.0.0.1", port=8600, transport=None, worker=True, https_proxy=False):
    """https_proxy=True when a tunnel (Tailscale Funnel, Cloudflare Tunnel) terminates HTTPS in front of this server:
    cookies get the Secure flag and HSTS is sent."""
    app = App(tenant, transport)
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    srv.tls = bool(https_proxy)
    srv.app = app
    if worker:
        threading.Thread(target=app.worker, daemon=True).start()
    return srv


def pid_path(tenant):
    return store.CLIENTS_DIR / f"{tenant}.pid"


def serve(tenant, host="127.0.0.1", port=8600, https_proxy=False):
    import sys
    if sys.stdout is None or sys.stderr is None:
        # started with pythonw (no window): keep a log so problems can still be read
        store.CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
        log = open(store.CLIENTS_DIR / f"{tenant}-server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    srv = make_server(tenant, host, port, https_proxy=https_proxy)
    # the Start/Stop scripts find this process by its id (window titles aren't reliable inside Windows Terminal)
    import os
    pid_path(tenant).write_text(str(os.getpid()), encoding="ascii")
    print(f"\n  AI SIDEKICKS - {tenant} assistant\n  Dashboard: http://{host}:{port}\n  Webhook:   <your public https address>/webhooks/instagram\n"
          + ("  Behind an HTTPS tunnel: secure cookies on.\n" if https_proxy else ""))
    con = srv.app.con
    if not con.execute("SELECT 1 FROM users").fetchone():
        url = f"http://127.0.0.1:{port}/setup#{new_setup_code(con)}"
        print("  First run: create the logins in the browser page that just opened (link valid 1 hour).\n")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            print("  Couldn't open the browser. Run the start script again.")
    print(f"  Instagram sending: {'ON' if store.flag(con, 'live_send') else 'OFF (test mode)'} - Reply mode: {store.get(con, 'reply_mode')} - "
          f"AI paused: {'yes' if store.flag(con, 'automation_paused') else 'no'}\n")
    print(f"  Started {time.strftime('%Y-%m-%d %H:%M:%S')} (process {__import__('os').getpid()})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.app.stop.set()
    finally:
        try:
            pid_path(tenant).unlink()
        except OSError:
            pass
