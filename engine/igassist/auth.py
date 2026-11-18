"""Sign-in for the client dashboard. Roles:
  owner     The business owner (Mola, for AI Sidekicks). Sees and runs everything; the only role that can finalize requests.
  operator  A helper account: knowledge base, integrations, troubleshooting. Can't approve requests.
Passwords are stored as scrypt hashes; session tokens are stored only as SHA-256 hashes.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from . import store

ROLES = ("owner", "operator")
SESSION_HOURS = 12
MAX_FAILS = 5
LOCK_MIN = 15


def _scrypt(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(pw):
    if len(pw or "") < 10:
        raise ValueError("Passwords need at least 10 characters.")
    salt = secrets.token_bytes(16)
    return "scrypt$" + salt.hex() + "$" + _scrypt(pw, salt).hex()


def check_password(pw, stored):
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(_scrypt(pw or "", bytes.fromhex(salt)).hex(), h)
    except (ValueError, AttributeError):
        return False


def add_user(con, username, name, role, password, actor="cli"):
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}.")
    u = (username or "").strip().lower()
    if not u or not u.replace("_", "").replace(".", "").isalnum():
        raise ValueError("Usernames are letters, digits, . or _.")
    con.execute("INSERT INTO users(username,name,role,pw_hash,created_at) VALUES(?,?,?,?,?)",
                (u, name or u, role, hash_password(password), store.now()))
    store.audit(con, actor, "user.add", u, {"role": role})
    con.commit()


def set_password(con, username, password, actor="cli"):
    n = con.execute("UPDATE users SET pw_hash=?, failed=0, locked_until=NULL WHERE username=?", (hash_password(password), username.lower())).rowcount
    con.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username.lower(),))
    store.audit(con, actor, "user.password", username)
    con.commit()
    return n == 1


def login(con, username, password, ip=""):
    """Returns (token, csrf, user) or raises ValueError with a message safe to show."""
    u = con.execute("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),)).fetchone()
    now = datetime.now()
    if u and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
        raise ValueError("Too many attempts. Try again in a few minutes.")
    if not u or u["disabled"] or not check_password(password, u["pw_hash"]):
        if u:
            fails = u["failed"] + 1
            lock = (now + timedelta(minutes=LOCK_MIN)).isoformat() if fails >= MAX_FAILS else None
            con.execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (0 if lock else fails, lock, u["id"]))
            store.audit(con, "auth", "login.failed", u["username"], {"ip": ip, "locked": bool(lock)})
            con.commit()
        raise ValueError("Wrong username or password.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    con.execute("UPDATE users SET failed=0, locked_until=NULL WHERE id=?", (u["id"],))
    con.execute("INSERT INTO sessions(token_hash,user_id,csrf,created_at,expires_at,last_seen,ip) VALUES(?,?,?,?,?,?,?)",
                (_h(token), u["id"], csrf, now.isoformat(), (now + timedelta(hours=SESSION_HOURS)).isoformat(), now.isoformat(), ip))
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    store.audit(con, u["username"], "login", u["username"], {"ip": ip})
    con.commit()
    return token, csrf, public(u)


def _h(t):
    return hashlib.sha256((t or "").encode()).hexdigest()


def session(con, token):
    if not token:
        return None
    r = con.execute("SELECT s.csrf, s.expires_at, u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (_h(token),)).fetchone()
    if not r or r["disabled"] or datetime.fromisoformat(r["expires_at"]) < datetime.now():
        return None
    return {**public(r), "csrf": r["csrf"]}


def logout(con, token):
    con.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))
    con.commit()


def public(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"]}
