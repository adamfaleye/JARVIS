"""Local SQLite store: leads, SMS contacts/messages, feed deals, run log."""
import json
import re
import sqlite3
from datetime import datetime
from .config import ROOT

DB_PATH = ROOT / "data" / "jarvis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, county TEXT, state TEXT, source_type TEXT,
  case_number TEXT, parcel_id TEXT, address TEXT, city TEXT, zip TEXT, owner TEXT,
  judgment_amount REAL, assessed_value REAL, event_date TEXT, url TEXT, raw TEXT,
  first_seen TEXT, status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY, phone TEXT UNIQUE, first_name TEXT, full_name TEXT, address TEXT,
  consent INTEGER DEFAULT 0, enrolled_at TEXT, step INTEGER DEFAULT 0, next_send TEXT,
  last_sent TEXT, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, phone TEXT, direction TEXT, body TEXT, sid TEXT UNIQUE, at TEXT);
CREATE TABLE IF NOT EXISTS feed_deals(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, address TEXT, county TEXT, asking REAL, arv REAL,
  beds TEXT, baths TEXT, sqft TEXT, condition TEXT, notes TEXT, source_file TEXT,
  first_seen TEXT, matches TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, kind TEXT, target TEXT, started TEXT, finished TEXT,
  found INTEGER, new INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now():
    return datetime.now().isoformat(timespec="seconds")


_ABBR = [("street", "st"), ("avenue", "ave"), ("road", "rd"), ("drive", "dr"), ("boulevard", "blvd"),
         ("lane", "ln"), ("court", "ct"), ("place", "pl"), ("terrace", "ter"), ("circle", "cir"),
         ("north", "n"), ("south", "s"), ("east", "e"), ("west", "w")]


def norm_addr(a):
    s = str(a or "").lower().split(",")[0]
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    for full, ab in _ABBR:
        s = re.sub(rf"\b{full}\b", ab, s)
    return re.sub(r"\s+", " ", s).strip()


def money(v):
    if v in (None, ""):
        return None
    try:
        return float(re.sub(r"[^0-9.]", "", str(v)) or "nan")
    except ValueError:
        return None


def insert_lead(con, rec):
    """Insert if new. Returns True when inserted."""
    key = (f"{rec.get('county','')}|{rec.get('state','')}|{rec.get('source_type','')}|case:{rec['case_number']}"
           if rec.get("case_number") else
           f"{rec.get('county','')}|{rec.get('state','')}|addr:{norm_addr(rec.get('address'))}")
    if not rec.get("address") and not rec.get("case_number"):
        return False
    cur = con.execute(
        """INSERT OR IGNORE INTO leads(key,county,state,source_type,case_number,parcel_id,address,city,zip,owner,
           judgment_amount,assessed_value,event_date,url,raw,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, rec.get("county"), rec.get("state"), rec.get("source_type"), rec.get("case_number"),
         rec.get("parcel_id"), rec.get("address"), rec.get("city"), rec.get("zip"), rec.get("owner"),
         money(rec.get("judgment_amount")), money(rec.get("assessed_value")), rec.get("event_date"),
         rec.get("url"), json.dumps(rec.get("raw", {}), default=str)[:20000], now()))
    return cur.rowcount == 1


def kv_get(con, k, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()

CRM_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_items(
  kind TEXT, id TEXT, data TEXT, ts TEXT DEFAULT '', deleted INTEGER DEFAULT 0,
  PRIMARY KEY(kind, id));
CREATE INDEX IF NOT EXISTS crm_ts ON crm_items(ts);
"""
_connect_base = connect


def connect():  # noqa: F811  (adds CRM mirror tables)
    con = _connect_base()
    con.executescript(CRM_SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    for c, t in [("campaign_id", "TEXT"), ("sender", "TEXT"), ("status", "TEXT"), ("error_code", "TEXT"), ("step", "INTEGER")]:
        if c not in have:  # campaign / number / delivery tracking
            con.execute(f"ALTER TABLE messages ADD COLUMN {c} {t}")
    try:  # CSV export is tracked separately so exported leads still reach the JARVIS app
        con.execute("ALTER TABLE leads ADD COLUMN exported_at TEXT")
        con.commit()
    except sqlite3.OperationalError:
        pass
    return con
