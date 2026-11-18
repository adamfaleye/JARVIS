"""Connects the AI Sidekicks assistant to Instagram: asks for the token and app secret, checks them with Meta,
finds the @aisidekicks account id by itself, and writes the AISIDEKICKS_IG_* lines into .env.
Everything is typed here on your PC only. Nothing is shown, logged or sent anywhere except Meta.
Run: double-click Set-Instagram-Keys.bat"""
import hashlib
import hmac
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"
P = "AISIDEKICKS_IG_"


def _existing_verify_token():
    """Keep the webhook verify token already in .env (Meta has it); make a fresh random one only on first setup.
    It is never written into the code, because the GitHub repo is public."""
    try:
        for l in ENV.read_text(encoding="utf-8").splitlines():
            if l.strip().upper().startswith(P + "VERIFY_TOKEN="):
                v = l.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    return None


VERIFY_TOKEN = _existing_verify_token() or ("aisidekicks-" + secrets.token_hex(12))
WANT = "aisidekicks"
FB = "https://graph.facebook.com/v26.0/"
IG = "https://graph.instagram.com/v26.0/"
FIELDS = "messages,messaging_postbacks,message_edit"


def clean(raw):
    t = (raw or "").strip().strip('"').strip("'").strip("<>").strip()
    t = re.sub(r"\s+", "", t)
    if "=" in t and t.split("=", 1)[0].upper().startswith(P):
        t = t.split("=", 1)[1]
    return t


def call(method, url, params):
    data = None
    q = urllib.parse.urlencode(params)
    if method == "GET":
        url = url + ("&" if "?" in url else "?") + q
    else:
        data = q.encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def err(body):
    return str((body.get("error") or {}).get("message") or "")[:200]


def find_account(token):
    """Returns (login, account_id, username, page) or raises SystemExit with a plain reason."""
    # Instagram Login token (graph.instagram.com)
    code, b = call("GET", IG + "me", {"fields": "user_id,username", "access_token": token})
    if code == 200 and (b.get("user_id") or b.get("id")):
        return "instagram", str(b.get("user_id") or b.get("id")), b.get("username") or "", None
    # Facebook / Unified Login token (graph.facebook.com): the Instagram account hangs off a Facebook Page
    code, b = call("GET", FB + "me/accounts", {"fields": "name,access_token,instagram_business_account{id,username}", "access_token": token})
    if code == 200:
        found = [(p, p["instagram_business_account"]) for p in b.get("data", []) if p.get("instagram_business_account")]
        found.sort(key=lambda x: (x[1].get("username") or "").lower() != WANT)
        if found:
            page, ig = found[0]
            return "unified", str(ig["id"]), ig.get("username") or "", page
    code2, me = call("GET", FB + "me", {"fields": "id,name", "access_token": token})
    if code2 != 200:
        sys.exit(f"Meta says this token isn't valid ({err(me) or err(b) or code2}). Generate it again and run this once more. Nothing was changed.")
    print("\nThe token works, but I couldn't find the Instagram account through it on my own.")
    acct = clean(input("Paste the Instagram account ID (the long number next to @aisidekicks on the token page): "))
    if not acct.isdigit():
        sys.exit("That isn't an account ID (it's only digits). Nothing was changed.")
    code, b = call("GET", FB + acct, {"fields": "id,username", "access_token": token})
    if code != 200:
        sys.exit(f"Meta won't show that account with this token ({err(b) or code}). Nothing was changed.")
    return "unified", acct, b.get("username") or "", None


def check_secret(token, secret):
    proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    code, b = call("GET", FB + "me", {"fields": "id", "access_token": token, "appsecret_proof": proof})
    return code == 200 or "appsecret_proof" not in err(b).lower()


def read_env():
    raw = ENV.read_bytes() if ENV.exists() else b""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def save(values, drop):
    lines = read_env().splitlines()
    removed_old = sum(1 for l in lines if l.strip().upper().startswith("CURLMEBELLA_"))
    keys = set(values) | set(drop)
    lines = [l for l in lines if not l.strip().upper().startswith("CURLMEBELLA_")
             and l.split("=", 1)[0].strip().upper() not in keys]
    lines = [l for l in lines if "curlmebella" not in l.lower() and "written by set-instagram-keys" not in l.lower()]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("# AI Sidekicks Instagram assistant (@aisidekicks), written by Set-Instagram-Keys")
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return removed_old


def main():
    print("Connect @aisidekicks to the AI Sidekicks assistant.")
    print("Right-click pastes in this window. Nothing you paste is shown anywhere else.\n")
    token = clean(input("1/2  Paste the access token, then press Enter: "))
    if len(token) < 40:
        sys.exit("\nThat's too short to be a Meta token. Copy it again and run this once more. Nothing was changed.")
    print("\nChecking the token with Meta...")
    try:
        login, acct, username, page = find_account(token)
    except urllib.error.URLError as e:
        sys.exit(f"Couldn't reach Meta ({e}). Check the internet connection and try again. Nothing was changed.")
    print(f"Found Instagram account @{username or '?'} (id ending {acct[-4:]}), "
          f"{'Facebook/Unified Login' if login == 'unified' else 'Instagram Login'} token.")
    if username and username.lower() != WANT:
        if input(f"That's @{username}, not @{WANT}. Use it anyway? (y/n): ").strip().lower() != "y":
            sys.exit("Nothing was changed. Generate the token while signed in as @aisidekicks.")

    where = ("Meta app > App settings > Basic > App secret (click Show)" if login == "unified"
             else "Meta app > Instagram > API setup with Instagram login > Instagram app secret")
    secret = ""
    for attempt in range(2):
        secret = clean(input(f"\n2/2  Paste the app secret ({where}): "))
        if not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
            print("An app secret is 32 letters and numbers (0-9, a-f). Copy it again.")
            continue
        if login == "unified" and not check_secret(token, secret):
            print("Meta says that secret doesn't belong to this app. Copy it again.")
            secret = ""
            continue
        break
    else:
        sys.exit("\nNothing was changed. Run this again with the right app secret.")
    if not secret:
        sys.exit("\nNothing was changed. Run this again with the right app secret.")

    values = {P + "ACCESS_TOKEN": token, P + "ACCOUNT_ID": acct, P + "APP_SECRET": secret, P + "VERIFY_TOKEN": VERIFY_TOKEN}
    drop = []
    if login == "instagram":
        values[P + "LOGIN"] = "instagram"
    else:
        drop.append(P + "LOGIN")
    removed = save(values, drop)
    print("\nSaved to .env." + (f" (Also removed {removed} old CurlMeBella line(s).)" if removed else ""))

    # Ask Meta to deliver this account's DMs to the app (the webhook URL itself is set in the Meta app dashboard).
    if page and page.get("access_token"):
        code, b = call("POST", FB + f"{page['id']}/subscribed_apps", {"subscribed_fields": "messages", "access_token": page["access_token"]})
        if code != 200:
            code, b = call("POST", FB + f"{page['id']}/subscribed_apps", {"subscribed_fields": "feed", "access_token": page["access_token"]})
    elif login == "instagram":
        code, b = call("POST", IG + "me/subscribed_apps", {"subscribed_fields": FIELDS, "access_token": token})
    else:
        code, b = call("POST", FB + f"{acct}/subscribed_apps", {"subscribed_fields": FIELDS, "access_token": token})
    print("DM delivery for this account: " + ("turned on." if code == 200 and b.get("success", True) else
                                             f"not confirmed ({err(b) or code}); it can be turned on later."))
    print("\nRestarting the assistant with the new keys...")


if __name__ == "__main__":
    main()
