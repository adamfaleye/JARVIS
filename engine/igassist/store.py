"""Tenant database for the client assistant. One SQLite file per client: data/clients/<tenant>.db."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT

CLIENTS_DIR = ROOT / "data" / "clients"
TENANT_RE = re.compile(r"[a-z0-9_]{2,40}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS kb_items(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
  version INTEGER DEFAULT 1, updated_at TEXT, updated_by TEXT, UNIQUE(kind, key));
CREATE TABLE IF NOT EXISTS kb_history(id INTEGER PRIMARY KEY, kind TEXT, key TEXT, data TEXT, version INTEGER, at TEXT, by TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT, role TEXT NOT NULL, pw_hash TEXT NOT NULL,
  created_at TEXT, disabled INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, locked_until TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, csrf TEXT NOT NULL, created_at TEXT, expires_at TEXT, last_seen TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, igsid TEXT UNIQUE NOT NULL, username TEXT, name TEXT,
  contact TEXT, notes TEXT, created_at TEXT, updated_at TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL UNIQUE, mode TEXT DEFAULT 'ai', status TEXT DEFAULT 'open',
  escalation_reason TEXT, classification TEXT, last_inbound_at TEXT, last_outbound_at TEXT, intro_sent INTEGER DEFAULT 0,
  unread INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, direction TEXT NOT NULL, author TEXT NOT NULL,
  text TEXT, attachments TEXT, mid TEXT UNIQUE, status TEXT NOT NULL, hold_reason TEXT, error TEXT,
  intent TEXT, ai_meta TEXT, ai_original TEXT, created_at TEXT, sent_at TEXT, attempts INTEGER DEFAULT 0,
  next_attempt_at TEXT, reply_to INTEGER, kind TEXT DEFAULT 'reply');
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS messages_status ON messages(status);
CREATE TABLE IF NOT EXISTS webhook_events(
  id INTEGER PRIMARY KEY, received_at TEXT, signature_ok INTEGER, body TEXT, processed_at TEXT, error TEXT,
  attempts INTEGER DEFAULT 0, source TEXT DEFAULT 'meta');
CREATE TABLE IF NOT EXISTS seen_events(key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, customer_id INTEGER NOT NULL, kind TEXT DEFAULT 'booking',
  status TEXT NOT NULL, version INTEGER DEFAULT 1, fields TEXT NOT NULL, summary TEXT, missing TEXT, questions TEXT,
  confidence REAL, escalation_reason TEXT, availability TEXT DEFAULT 'awaiting', verified_slot TEXT,
  approved_hash TEXT, approved_by TEXT, approved_at TEXT, calendar_entered_at TEXT, calendar_entered_by TEXT, calendar_ref TEXT,
  confirmed_at TEXT, confirmed_by TEXT, confirmation_message_id INTEGER, decline_reason TEXT,
  created_at TEXT, updated_at TEXT, draft_created_at TEXT, is_test INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS appointments_status ON appointments(status);
CREATE TABLE IF NOT EXISTS appointment_events(
  id INTEGER PRIMARY KEY, appointment_id INTEGER NOT NULL, at TEXT, actor TEXT, action TEXT, from_status TEXT, to_status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS appointment_notes(id INTEGER PRIMARY KEY, appointment_id INTEGER NOT NULL, at TEXT, author TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS integration_health(name TEXT PRIMARY KEY, ok INTEGER, at TEXT, detail TEXT);
"""

DEFAULT_SETTINGS = {
    "automation_paused": "0",       # global pause: AI writes and sends nothing while on
    "reply_mode": "review",         # review: AI replies wait for the owner's tap; auto: approved routine replies go out on their own
    "live_send": "0",               # nothing reaches Instagram until this is on (after the owner approves the pilot)
    "pilot_approved": "",           # timestamp, set only by the owner
    "takeover_on_echo": "1",        # the owner replying from the Instagram app pauses the AI in that conversation
    "intro_line": "1",              # first AI reply says it's the business's assistant (honest, not a disclaimer wall)
    "voice_person": "assistant",    # assistant: speaks for the owner in the third person; first_person only if the owner approves it
    "minutes_per_reply": "2",       # used only for the labeled "estimated time saved" figure
    "retention_days": "",           # empty = keep; otherwise messages older than N days are deleted by the retention job
    "inbox_poll": "1",              # also check the Instagram inbox directly every 20 s (works when Meta sends no webhooks)
    "calendar_mode": "manual",      # manual: the owner enters approved requests in their calendar; no calendar integration yet
}


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def db_path(tenant):
    if not TENANT_RE.fullmatch(tenant or ""):
        raise ValueError("Tenant ids are 2-40 lowercase letters, digits or _.")
    return CLIENTS_DIR / f"{tenant}.db"


def connect(tenant):
    p = db_path(tenant)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError:
        pass
    con.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, v))
    con.execute("INSERT OR IGNORE INTO settings(k,v) VALUES('tenant',?)", (tenant,))
    con.commit()
    return con


def tenant_of(con):
    return get(con, "tenant")


def get(con, k, default=None):
    r = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def put(con, k, v, actor="system"):
    old = get(con, k)
    con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    if str(old) != str(v):
        audit(con, actor, "setting", k, {"from": old, "to": str(v)})
    con.commit()


def flag(con, k):
    return str(get(con, k, "0")) in ("1", "true", "yes", "on")


def audit(con, actor, action, target="", detail=None):
    con.execute("INSERT INTO audit(at,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (now(), actor, action, str(target or ""), json.dumps(detail or {}, default=str)[:8000]))


def health(con, name, ok, detail=""):
    con.execute("INSERT INTO integration_health(name,ok,at,detail) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "ok=excluded.ok, at=excluded.at, detail=excluded.detail", (name, 1 if ok else 0, now(), str(detail)[:500]))
    con.commit()


def j(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def backup(tenant, dest_dir=None):
    """Consistent copy of the tenant database (SQLite online backup). Returns the file path."""
    src = connect(tenant)
    d = Path(dest_dir) if dest_dir else CLIENTS_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{tenant}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    dst.close()
    return out
