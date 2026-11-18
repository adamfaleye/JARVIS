"""Official Meta Instagram messaging API, API v26.0. Two setups are supported:

  unified    (default) Instagram API with Unified Login. Meta gives ONLY this to apps created since late Oct 2026.
             Host graph.facebook.com; permissions instagram_basic + instagram_manage_messages; the token is a business
             system-user token (doesn't expire); the account is addressed by its ID (POST /<IG_ACCOUNT_ID>/messages).
  instagram  Instagram API with Instagram Login (older apps). Host graph.instagram.com; /me works; 60-day tokens.
  Set <TENANT>_IG_LOGIN=instagram in .env only for an older app.


Verified against Meta's docs on Nov 16, 2026:
- Permissions: instagram_business_basic, instagram_business_manage_messages. Instagram professional account (Business or Creator).
- Webhooks: GET verification with hub.mode / hub.verify_token / hub.challenge; POSTs signed with X-Hub-Signature-256
  (sha256=HMAC of the raw body with the app secret). Meta retries failed deliveries for up to 36 hours, so every
  event is deduplicated. A valid public TLS certificate is required (self-signed isn't accepted).
- Send: POST /<IG_ID>/messages {recipient: {id: IGSID}, message: {text}}; text up to 1000 bytes; only within 24 hours
  of the customer's last message (a separate human-agent tag exists but needs its own permission; not used).
Secrets come from .env (never the database or the browser):
  <TENANT>_IG_APP_SECRET, <TENANT>_IG_VERIFY_TOKEN, <TENANT>_IG_ACCESS_TOKEN, <TENANT>_IG_ACCOUNT_ID
"""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import store

GRAPH = "https://graph.instagram.com"          # Instagram Login
GRAPH_UNIFIED = "https://graph.facebook.com"   # Unified Login
VERSION = "v26.0"
NON_RETRYABLE = {10, 100, 190, 200, 551, 2018278, 2534022}


def env(tenant, name):
    return (os.environ.get(f"{tenant.upper()}_IG_{name}") or "").strip()


def login_type(tenant):
    return "instagram" if env(tenant, "LOGIN").lower() == "instagram" else "unified"


def configured(tenant):
    return {k: bool(env(tenant, k)) for k in ("APP_SECRET", "VERIFY_TOKEN", "ACCESS_TOKEN", "ACCOUNT_ID")}


def verify_challenge(tenant, params):
    """GET /webhooks/instagram. Returns the challenge string, or None (403)."""
    token = env(tenant, "VERIFY_TOKEN")
    if params.get("hub.mode") == "subscribe" and token and hmac.compare_digest(params.get("hub.verify_token", ""), token):
        return params.get("hub.challenge", "")
    return None


def signature_ok(tenant, raw_body, header):
    """Meta signs with the app secret. Unified Login apps sign with the Meta app secret (App settings > Basic);
    Instagram Login apps with the Instagram app secret. Either configured secret is accepted."""
    secrets_ = [x for x in (env(tenant, "APP_SECRET"), env(tenant, "META_APP_SECRET")) if x]
    if not secrets_ or not header or not header.startswith("sha256="):
        return False
    got = header[7:].strip()
    return any(hmac.compare_digest(hmac.new(sec.encode(), raw_body, hashlib.sha256).hexdigest(), got) for sec in secrets_)


def store_event(con, raw_body, sig_ok, source="meta"):
    cur = con.execute("INSERT INTO webhook_events(received_at,signature_ok,body,source) VALUES(?,?,?,?)",
                      (store.now(), 1 if sig_ok else 0, raw_body.decode("utf-8", "replace")[:200000], source))
    con.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ turning events into conversation records
def _customer(con, igsid, is_test=0):
    r = con.execute("SELECT * FROM customers WHERE igsid=?", (igsid,)).fetchone()
    if r:
        return r
    con.execute("INSERT INTO customers(igsid,created_at,updated_at,is_test) VALUES(?,?,?,?)", (igsid, store.now(), store.now(), is_test))
    return con.execute("SELECT * FROM customers WHERE igsid=?", (igsid,)).fetchone()


def _conversation(con, customer):
    r = con.execute("SELECT * FROM conversations WHERE customer_id=?", (customer["id"],)).fetchone()
    if r:
        return r
    con.execute("INSERT INTO conversations(customer_id,created_at,updated_at,is_test) VALUES(?,?,?,?)",
                (customer["id"], store.now(), store.now(), customer["is_test"]))
    return con.execute("SELECT * FROM conversations WHERE customer_id=?", (customer["id"],)).fetchone()


def _seen(con, key):
    return con.execute("INSERT OR IGNORE INTO seen_events(key,at) VALUES(?,?)", (key, store.now())).rowcount == 0


def _items(payload):
    for entry in (payload or {}).get("entry") or []:
        for ev in entry.get("messaging") or []:
            yield entry.get("id"), ev
        for ch in entry.get("changes") or []:  # dashboard "Test" button and some deliveries use changes[]
            if ch.get("field") in ("messages", "message_edit", "messaging_postbacks") and isinstance(ch.get("value"), dict):
                yield entry.get("id"), ch["value"]


def process_payload(con, payload, *, profile_fetch=None, is_test=0):
    """Returns [(conversation_id, message_id)] for new inbound customer messages that need a reply."""
    todo = []
    for account_id, ev in _items(payload):
        msg = ev.get("message")
        sender = str((ev.get("sender") or {}).get("id") or "")
        recipient = str((ev.get("recipient") or {}).get("id") or "")
        ts = ev.get("timestamp")
        if ev.get("message_edit"):
            me = ev["message_edit"]
            if me.get("mid") and not _seen(con, f"edit:{me['mid']}:{me.get('num_edit', '')}"):
                con.execute("UPDATE messages SET text=?, ai_meta=json_set(COALESCE(ai_meta,'{}'),'$.edited',1) WHERE mid=? AND direction='in'",
                            (str(me.get("text") or "")[:5000], me["mid"]))
            continue
        if ev.get("postback"):
            pb = ev["postback"]
            msg = {"mid": pb.get("mid") or f"pb:{sender}:{ts}", "text": pb.get("title") or pb.get("payload") or ""}
        if not msg or not msg.get("mid"):
            continue
        mid = str(msg["mid"])
        if msg.get("is_deleted"):
            # Meta Platform Terms: delete a message when the person deletes it
            con.execute("UPDATE messages SET text=NULL, attachments=NULL, status='deleted' WHERE mid=?", (mid,))
            continue
        if msg.get("is_echo"):
            igsid = recipient
            if con.execute("SELECT 1 FROM messages WHERE mid=?", (mid,)).fetchone() or _seen(con, f"mid:{mid}"):
                continue  # our own API send coming back
            cust = _customer(con, igsid, is_test)
            conv = _conversation(con, cust)
            con.execute("INSERT INTO messages(conversation_id,direction,author,text,attachments,mid,status,created_at,sent_at,kind) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (conv["id"], "out", "owner", msg.get("text"), json.dumps(msg.get("attachments") or []), mid,
                                                         "sent", store.now(), store.now(), "instagram_app"))
            con.execute("UPDATE conversations SET last_outbound_at=?, updated_at=? WHERE id=?", (store.now(), store.now(), conv["id"]))
            if store.flag(con, "takeover_on_echo") and conv["mode"] != "human":
                con.execute("UPDATE conversations SET mode='human', updated_at=? WHERE id=?", (store.now(), conv["id"]))
                con.execute("UPDATE messages SET status='discarded', hold_reason='The owner replied from the Instagram app' "
                            "WHERE conversation_id=? AND author='ai' AND status IN ('draft','queued')", (conv["id"],))
                store.audit(con, "instagram", "conversation.takeover", conv["id"], {"why": "The owner replied from the Instagram app"})
            continue
        if _seen(con, f"mid:{mid}"):
            continue  # duplicate delivery (Meta retries)
        if not sender or sender == str(account_id or ""):
            continue
        cust = _customer(con, sender, is_test)
        if profile_fetch and not cust["username"]:
            try:
                p = profile_fetch(sender) or {}
                con.execute("UPDATE customers SET username=COALESCE(?,username), name=COALESCE(name,?), updated_at=? WHERE id=?",
                            (p.get("username"), p.get("name"), store.now(), cust["id"]))
            except Exception:  # noqa: BLE001  (profile is a nice-to-have)
                pass
        conv = _conversation(con, cust)
        atts = []
        for a in msg.get("attachments") or []:
            atts.append({"type": a.get("type"), "url": ((a.get("payload") or {}).get("url"))})
        text = msg.get("text") or ("" if atts else "[unsupported message]" if msg.get("is_unsupported") else "")
        cur = con.execute("INSERT INTO messages(conversation_id,direction,author,text,attachments,mid,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (conv["id"], "in", "customer", text, json.dumps(atts), mid, "received", store.now()))
        con.execute("UPDATE conversations SET last_inbound_at=?, updated_at=?, unread=unread+1, status=CASE WHEN status='closed' THEN 'open' ELSE status END WHERE id=?",
                    (store.now(), store.now(), conv["id"]))
        todo.append((conv["id"], cur.lastrowid))
    con.commit()
    return todo


# ------------------------------------------------------------------ Graph API transport
class SendError(Exception):
    def __init__(self, msg, retryable=True, code=None):
        super().__init__(msg)
        self.retryable = retryable
        self.code = code


class GraphTransport:
    def __init__(self, tenant, opener=None):
        self.tenant = tenant
        self.token = env(tenant, "ACCESS_TOKEN")
        self.login = login_type(tenant)
        self.host = GRAPH_UNIFIED if self.login == "unified" else GRAPH
        # Unified Login tokens address the account by its ID; /me only works with Instagram Login
        self.account = env(tenant, "ACCOUNT_ID") or ("" if self.login == "unified" else "me")
        self.opener = opener or urllib.request.urlopen

    def _call(self, method, path, body=None, params=None, token=None):
        if not self.token:
            raise SendError("Instagram isn't connected (no access token in .env).", retryable=False)
        url = f"{self.host}/{VERSION}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token or self.token}"})
        try:
            with self.opener(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode() or "{}").get("error") or {}
            except ValueError:
                err = {}
            code, sub = err.get("code"), err.get("error_subcode")
            retry = e.code >= 500 or e.code == 429 or code in (1, 2, 4, 17, 32, 613)
            if code in NON_RETRYABLE or sub in NON_RETRYABLE:
                retry = False
            raise SendError(f"Instagram error {e.code}: {err.get('message') or e.reason} (code {code}{'/' + str(sub) if sub else ''})", retry, code)
        except urllib.error.URLError as e:
            raise SendError(f"Network error reaching Instagram: {e.reason}", True)

    def send_text(self, igsid, text):
        if str(igsid).startswith("test:"):
            raise SendError("Test conversations are never sent to Instagram.", retryable=False)
        if not self.account:
            raise SendError("Instagram account id missing in .env (needed for Unified Login).", retryable=False)
        r = self._call("POST", f"{self.account}/messages", {"recipient": {"id": igsid}, "message": {"text": text}})
        return r.get("message_id")

    def profile(self, igsid):
        if str(igsid).startswith("test:"):
            return {}
        return self._call("GET", igsid, params={"fields": "name,username"})

    def me(self):
        if self.login == "unified":
            return self._call("GET", self.account or "me", params={"fields": "id,username,name"})
        return self._call("GET", "me", params={"fields": "user_id,username,account_type"})

    def subscribe(self, fields=("messages", "messaging_postbacks", "message_edit")):
        return self._call("POST", f"{self.account or 'me'}/subscribed_apps", params={"subscribed_fields": ",".join(fields)})

    def _page(self):
        """Unified Login: the Facebook Page linked to the Instagram account, and that Page's token (cached)."""
        if getattr(self, "_page_cache", None):
            return self._page_cache
        r = self._call("GET", "me/accounts", params={"fields": "id,access_token,instagram_business_account"})
        for p in r.get("data", []):
            if str((p.get("instagram_business_account") or {}).get("id") or "") == str(self.account):
                self._page_cache = (p["id"], p.get("access_token") or self.token)
                return self._page_cache
        return None

    def my_username(self):
        if getattr(self, "_me_cache", None) is None:
            try:
                self._me_cache = str(self.me().get("username") or "").lower()
            except SendError:
                self._me_cache = ""
        return self._me_cache

    def recent_messages(self, per_conversation=10):
        """The newest messages in the account's Instagram inbox, via the Conversations API (no webhook needed).
        Returns [{mid, from, from_username, to, text, created, attachments}] across recent conversations."""
        fields = (f"id,updated_time,messages.limit({int(per_conversation)})"
                  "{id,from,to,message,created_time,attachments}")
        params = {"platform": "instagram", "fields": fields, "limit": 20}
        if self.login == "unified":
            page = self._page()
            if page:
                r = self._call("GET", f"{page[0]}/conversations", params=params, token=page[1])
            else:
                r = self._call("GET", f"{self.account}/conversations", params=params)
        else:
            r = self._call("GET", "me/conversations", params=params)
        out = []
        for conv in r.get("data", []):
            for m in ((conv.get("messages") or {}).get("data") or []):
                atts = []
                for a in ((m.get("attachments") or {}).get("data") or []):
                    url = ((a.get("image_data") or {}).get("url") or (a.get("video_data") or {}).get("url") or a.get("file_url"))
                    atts.append({"type": "image" if a.get("image_data") else "video" if a.get("video_data") else "file", "payload": {"url": url}})
                frm = m.get("from") or {}
                out.append({"mid": str(m.get("id") or ""), "from": str(frm.get("id") or ""), "from_username": (frm.get("username") or "").lower(),
                            "to": [str(x.get("id") or "") for x in ((m.get("to") or {}).get("data") or [])],
                            "text": m.get("message") or "", "created": m.get("created_time") or "", "attachments": atts})
        return out

    def refresh_token(self):
        if self.login == "unified":
            raise SendError("Unified Login tokens don't expire; nothing to refresh.", retryable=False)
        url = f"{GRAPH}/refresh_access_token?" + urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": self.token})
        with self.opener(urllib.request.Request(url), timeout=20) as r:
            return json.loads(r.read().decode())


class TestTransport:
    """Used for simulator conversations and tests: records, never touches the network."""

    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail  # callable(igsid, text) -> Exception or None

    def send_text(self, igsid, text):
        if self.fail:
            e = self.fail(igsid, text)
            if e:
                raise e
        self.sent.append((igsid, text))
        return f"test-mid-{uuid.uuid4().hex}"

    def profile(self, igsid):
        return {}
