"""Saves your ElevenLabs API key into .env after checking it can run voice agents.
The key is typed here on your PC only. It is never shown, logged or sent anywhere except ElevenLabs.
Run: double-click Set-ElevenLabs-Key.bat"""
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"
NAME = "ELEVENLABS_API_KEY"


def clean(raw):
    t = (raw or "").strip().strip('"').strip("'").strip("<>").strip()
    t = re.sub(r"\s+", "", t)
    if t.upper().startswith(NAME + "="):
        t = t.split("=", 1)[1]
    return t


def get(path, key):
    req = urllib.request.Request("https://api.elevenlabs.io" + path, headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body


def save(t):
    raw = ENV.read_bytes() if ENV.exists() else b""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = [l for l in text.splitlines() if not l.strip().lstrip("﻿").upper().startswith(NAME)]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"{NAME}={t}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    print("Paste your ElevenLabs API key (right-click to paste in this window), then press Enter.")
    print("It needs: Text to Speech = Access, ElevenLabs Agents = Write, Voices = Read.\n")
    t = clean(input("Key: "))
    if len(t) < 20:
        print("\nThat's too short to be an ElevenLabs key. Copy it again and run this once more. Nothing was changed.")
        return
    print("\nChecking it with ElevenLabs...")
    try:
        code, _ = get("/v1/voices?page_size=1", t)
        if code in (401, 403) and get("/v1/models", t)[0] in (401, 403):
            print("ElevenLabs says this key isn't valid. Copy it again (or make a new one). Nothing was changed.")
            return
        code, body = get("/v1/convai/agents?page_size=1", t)
    except Exception as e:
        print(f"Couldn't reach ElevenLabs ({e}). Check the internet connection and try again. Nothing was changed.")
        return
    if code in (401, 403):
        detail = json.dumps(body.get("detail", ""))[:200]
        print("The key works, but it isn't allowed to use voice agents.")
        print("In ElevenLabs: Developers -> API Keys -> edit this key -> set 'ElevenLabs Agents' to Write, save, and run this again.")
        print(f"(ElevenLabs said: {detail}) Nothing was changed.")
        return
    if code != 200:
        print(f"ElevenLabs answered {code}; try again in a minute. Nothing was changed.")
        return
    save(t)
    print("\nWorks: this key can run voice agents. Saved to .env (the old ElevenLabs line was replaced).")
    print("Now run Stop-JARVIS, then Start-JARVIS, and tell Claude 'done'.")


if __name__ == "__main__":
    main()
