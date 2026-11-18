"""Saves your Telegram bot token into .env, the right way, after checking it with Telegram.
The token is typed here on your PC only. It is never shown, logged or sent anywhere except Telegram itself.
Run: double-click Set-Telegram-Token.bat"""
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"
SHAPE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")


def clean(raw):
    t = (raw or "").strip().strip('"').strip("'").strip("<>").strip()
    t = re.sub(r"\s+", "", t)
    if t.upper().startswith("TELEGRAM_BOT_TOKEN="):
        t = t.split("=", 1)[1]
    if t.lower().startswith("bot") and SHAPE.match(t[3:]):
        t = t[3:]
    return t


def check(t):
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getMe", timeout=20) as r:
            d = json.loads(r.read().decode())
        return d.get("result", {}).get("username") if d.get("ok") else None
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return None
        raise


def save(t):
    raw = ENV.read_bytes() if ENV.exists() else b""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = [l for l in text.splitlines() if not l.strip().lstrip("﻿").upper().startswith("TELEGRAM_BOT_TOKEN")]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"TELEGRAM_BOT_TOKEN={t}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print("Paste your bot token from @BotFather (right-click to paste in this window), then press Enter.")
    print("In BotFather: /mybots -> your bot -> API Token.\n")
    t = clean(input("Token: "))
    if not SHAPE.match(t):
        print("\nThat doesn't look like a bot token. It should be numbers, a colon, then about 35 letters/numbers,")
        print("like 7123456789:AAH... Copy it again from BotFather and run this once more. Nothing was changed.")
        return
    print("\nChecking it with Telegram...")
    try:
        name = check(t)
    except Exception as e:
        print(f"Couldn't reach Telegram ({e}). Check the internet connection and try again. Nothing was changed.")
        return
    if not name:
        print("Telegram says this token isn't valid (it may be old: BotFather makes a new one each time you press")
        print("Revoke). Copy the current one from /mybots -> your bot -> API Token. Nothing was changed.")
        return
    save(t)
    print(f"\nWorks: your bot is @{name}. Saved to .env (any old Telegram line was replaced).")
    print("Now run Stop-JARVIS, then Start-JARVIS. Then tell Claude 'done' for your pairing code.")


if __name__ == "__main__":
    main()
