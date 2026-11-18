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
    # Two processes share this file: the engine loop and the PC-link server Claude runs. Wait for each other instead of
    # failing with "database is locked", and use WAL so reading never blocks on a write.
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        pass
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
    if cur.rowcount == 1:
        return True
    # already on file: a sale that was postponed to a later date comes back onto the review list
    new_date = str(rec.get("event_date") or "")
    if new_date:
        con.execute("UPDATE leads SET event_date=?, status=CASE WHEN status='expired' THEN 'new' ELSE status END "
                    "WHERE key=? AND COALESCE(event_date,'') < ?", (new_date, key, new_date))
    return False


# County leads whose sale date hasn't passed (or has no date). Used by everything that lists leads waiting for review.
UPCOMING = "(event_date IS NULL OR event_date = '' OR event_date >= date('now','localtime'))"


def expire_past(con, today=None):
    """Leads still waiting for review whose auction or sale date has passed are set aside (status 'expired') so the
    review list only shows sales that can still be worked. A postponed sale that the scraper sees again with a new date
    comes back automatically (insert_lead). Returns how many were set aside."""
    day = today or datetime.now().date().isoformat()
    cur = con.execute("UPDATE leads SET status='expired' WHERE status IN ('new','exported') AND event_date IS NOT NULL "
                      "AND event_date != '' AND event_date < ?", (day,))
    con.commit()
    return cur.rowcount


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
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    # exported_at: CSV export is tracked separately so exported leads still reach the JARVIS app
    # owner_info / owner_checked: owner looked up from the county appraiser (engine/owner_lookup.py)
    for c in ("exported_at", "owner_info", "owner_checked"):
        if c not in have:
            try:
                con.execute(f"ALTER TABLE leads ADD COLUMN {c} TEXT")
                con.commit()
            except sqlite3.OperationalError:
                pass
    return con
