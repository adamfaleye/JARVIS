"""JARVIS Engine command line.
  python run.py scrape [--county NAME]     scrape county records now
  python run.py export                     write JARVIS import + skip-trace CSVs for new leads
  python run.py add-county NAME --state ST [--url URL] [--instructions TEXT]
  python run.py counties                   list sources and last results
  python run.py sms enroll FILE.csv [--consent]
  python run.py sms send                   send due sequence texts
  python run.py sms poll                   pull replies (STOP → DNC, replies → out/hot_replies.csv)
  python run.py feed                       process WhatsApp exports in inbox/feed
  python run.py loop                       run everything on a schedule (leave this window open)
  python run.py voice [--device N]         hands-free JARVIS ("Hey Jarvis")
  python run.py voice --list-devices       show microphones
  python run.py web [--local] [--port N]   HQ on your network for the Meta Quest (see VR.md)
  python run.py web --pair                 new pairing code for a headset or browser
  python run.py web --revoke               unpair every device
"""
import argparse
import sys
import json
import threading
import time
from datetime import datetime
from engine import config, scrape, sms, feed, db


def main():
    ap = argparse.ArgumentParser(description="JARVIS Engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scrape"); s.add_argument("--county")
    sub.add_parser("export")
    a = sub.add_parser("add-county"); a.add_argument("name"); a.add_argument("--state", required=True)
    a.add_argument("--url"); a.add_argument("--adapter"); a.add_argument("--instructions")
    sub.add_parser("counties")
    m = sub.add_parser("sms"); m.add_argument("action", choices=["enroll", "send", "poll", "status"])
    m.add_argument("file", nargs="?"); m.add_argument("--consent", action="store_true")
    sub.add_parser("feed")
    sub.add_parser("loop")
    ow = sub.add_parser("owners", help="look up owners for county leads now (county appraiser records)"); ow.add_argument("--limit", type=int, default=80)
    ck = sub.add_parser("clerk", help="pull Pinellas Clerk pre-foreclosure, probate and eviction filings into The Hunter now")
    ck.add_argument("--category", choices=["preforeclosure", "probate", "eviction"])
    sub.add_parser("voice-calls", help="set up live voice calls: one ElevenLabs voice agent per employee (needs cloudflared + ELEVENLABS_API_KEY)")
    bp = sub.add_parser("brief", help="pre-call brief on an AI Sidekicks prospect (company, email or website)"); bp.add_argument("who", nargs="+")
    sub.add_parser("gmail-auth", help="connect JARVIS Engine to your Gmail (one time; you sign in in your browser)")
    wp = sub.add_parser("web"); wp.add_argument("--port", type=int, default=8443); wp.add_argument("--local", action="store_true")
    wp.add_argument("--http", action="store_true"); wp.add_argument("--pair", action="store_true"); wp.add_argument("--revoke", action="store_true")
    vp = sub.add_parser("voice"); vp.add_argument("--device", type=int); vp.add_argument("--list-devices", action="store_true")
    bl = sub.add_parser("assistant", help="AI Sidekicks Instagram DM assistant (DMs + discovery-call approvals). See AISIDEKICKS.md")
    bl.add_argument("action", choices=["init", "add-user", "password", "serve", "tick", "status", "backup", "meta-check", "meta-subscribe", "refresh-token"])
    bl.add_argument("--tenant", default="aisidekicks"); bl.add_argument("--username"); bl.add_argument("--name"); bl.add_argument("--role", choices=["owner", "operator"])
    bl.add_argument("--port", type=int, default=8600); bl.add_argument("--host", default="127.0.0.1")
    bl.add_argument("--https-proxy", action="store_true", help="a tunnel (Tailscale Funnel) serves this over HTTPS: secure cookies")
    args = ap.parse_args()
    cfg = config.load()

    if args.cmd == "scrape":
        scrape.run(cfg, args.county)
        scrape.export()
    elif args.cmd == "export":
        scrape.export()
    elif args.cmd == "add-county":
        added = scrape.add_county(args.name, args.state, args.url, args.adapter, args.instructions)
        if added:
            for x in added:
                print(f"Added {x['name']} {x['state']}: {x['url']} ({x.get('sale_type') or x['adapter']})")
            print("Run: python run.py scrape --county " + f'"{args.name}"')
        else:
            print("No RealAuction site found for that county. Find the clerk's foreclosure / lis pendens page and run:\n"
                  f'  python run.py add-county "{args.name}" --state {args.state} --url <page url> --instructions "what the page lists"')
    elif args.cmd == "counties":
        con = db.connect()
        for c in cfg["counties"]:
            label = f"{c['name']} {c.get('state','')} ({c.get('sale_type') or c.get('adapter')})"
            r = con.execute("SELECT finished,found,new,error FROM runs WHERE target=? ORDER BY id DESC LIMIT 1", (label,)).fetchone()
            last = f"last {r['finished']}: {r['found']} found, {r['new']} new{' ERROR ' + r['error'] if r['error'] else ''}" if r else "never run"
            print(f"{'ON ' if c.get('enabled', True) else 'OFF'} {label:45} {last}")
    elif args.cmd == "sms":
        if args.action == "enroll":
            if not args.file:
                sys.exit("Give a CSV path: python run.py sms enroll contacts.csv")
            sms.enroll(cfg, args.file, args.consent)
        elif args.action == "send":
            sms.tick(cfg)
        elif args.action == "poll":
            sms.poll(cfg)
        else:
            print(sms.overview())
    elif args.cmd == "feed":
        feed.process(cfg) or print("No new exports in inbox/feed.")
    elif args.cmd == "clerk":
        from engine import clerk_pinellas
        clerk_pinellas.sync(cfg, [args.category] if getattr(args, "category", None) else None)
    elif args.cmd == "voice-calls":
        from engine import voice_bridge
        cfg = config.load()
        print("Setting up live voice calls (one ElevenLabs voice agent per employee)...")
        v = cfg.get("voice") or {}
        voices = dict(v.get("agent_voices") or {})
        if v.get("elevenlabs_voice_id"):
            voices.setdefault("jarvis", str(v["elevenlabs_voice_id"]))
        voice_bridge.setup(voices=voices)
        print("Done. Restart JARVIS (Stop-JARVIS, Start-JARVIS); then text your bot \"call\" for your private call link."
              + ("" if voice_bridge.find_cloudflared() else "\nNote: cloudflared isn't installed yet. Run: winget install --id Cloudflare.cloudflared"))
    elif args.cmd == "brief":
        from engine import hq, precall
        who = " ".join(args.who)
        r = precall.build(hq.connect(), who=None if "." in who and " " not in who else who, website=who if "." in who and " " not in who else None)
        print(json.dumps(r.get("brief") or r, indent=2))
    elif args.cmd == "owners":
        from engine import owner_lookup
        owner_lookup.enrich(db.connect(), limit=args.limit)
    elif args.cmd == "loop":
        loop(cfg)
    elif args.cmd == "gmail-auth":
        from engine import gmail, hq
        if not gmail.client_path().exists():
            sys.exit(f"Put your Google OAuth client file (Desktop app) at {gmail.client_path()} first. See README: Engine email.")
        p = gmail.authorize()
        qc = hq.connect()
        db.kv_set(qc, "di_gmail_account", p.get("emailAddress", "")); qc.commit()
        print(f"Connected as {p.get('emailAddress')}. JARVIS Engine will now send approved AI Sidekicks emails and check replies while it runs.")
    elif args.cmd == "web":
        from engine import web
        if args.revoke:
            print(f"Unpaired {web.revoke_all()} device(s).")
        elif args.pair:
            c = web.new_pairing_code()
            print(f"Pairing code (15 minutes): {c[:3]} {c[3:]}")
        else:
            if args.http and not args.local:
                sys.exit("Plain HTTP is only allowed with --local (WebXR needs HTTPS on your network).")
            web.serve(args.port, args.local, not args.http)
    elif args.cmd == "assistant":
        assistant(args)
    elif args.cmd == "voice":
        from voice import jarvis_voice
        jarvis_voice.list_devices() if args.list_devices else jarvis_voice.main(args.device)


def assistant(args):
    """AI Sidekicks Instagram assistant. Nothing here sends a customer message or books anything."""
    import getpass
    from engine.igassist import auth, meta, service, store, workflow, analytics
    t = args.tenant
    if args.action == "init":
        con, added = service.init_tenant(t)
        print(f"Workspace '{t}' ready at {store.db_path(t)} ({added} knowledge entries added). Next: python run.py assistant serve (a browser page creates the login)")
    elif args.action in ("add-user", "password"):
        if not args.username:
            sys.exit("Give --username")
        pw = getpass.getpass("Password (10+ characters): ")
        if pw != getpass.getpass("Again: "):
            sys.exit("Passwords didn't match.")
        con = store.connect(t)
        if args.action == "add-user":
            if not args.role:
                sys.exit("Give --role owner or --role operator")
            auth.add_user(con, args.username, args.name or args.username, args.role, pw)
            print(f"Added {args.username} ({args.role}).")
        else:
            print("Password changed." if auth.set_password(con, args.username, pw) else "No such user.")
    elif args.action == "serve":
        from engine.igassist import server
        server.serve(t, args.host, args.port, https_proxy=args.https_proxy)
    elif args.action == "tick":
        con = store.connect(t)
        print(json.dumps(service.tick(con, t, meta.GraphTransport(t)), indent=2))
    elif args.action == "status":
        con = store.connect(t)
        q = workflow.queue(con)
        print(f"Needs you: {q['attention']}")
        for g in q["groups"]:
            print(f"  {g['label']}: {len(g['items'])}")
        print("Instagram .env:", meta.configured(t), "| sending:", "ON" if store.flag(con, "live_send") else "OFF (test mode)",
              "| AI paused:", store.flag(con, "automation_paused"), "| reply mode:", store.get(con, "reply_mode"))
        print(json.dumps({k: v for k, v in analytics.report(con, 30).items() if k not in ("baseline",)}, indent=2))
    elif args.action == "backup":
        print(f"Backup written: {store.backup(t)}")
    elif args.action == "meta-check":
        tr = meta.GraphTransport(t)
        print("Configured:", meta.configured(t))
        try:
            print("Account:", tr.me())
        except Exception as e:  # noqa: BLE001
            print("Instagram check failed:", e)
    elif args.action == "meta-subscribe":
        print(meta.GraphTransport(t).subscribe())
    elif args.action == "refresh-token":
        r = meta.GraphTransport(t).refresh_token()
        env_path = config.ROOT / ".env"
        key = f"{t.upper()}_IG_ACCESS_TOKEN"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        lines = [ln for ln in lines if not ln.startswith(key + "=")] + [f"{key}={r['access_token']}"]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Token refreshed; valid for about {int(r.get('expires_in', 0)) // 86400} days. Restart 'assistant serve' to use it.")


def maybe_start_scrape(cfg, con, now, state, hour):
    """The daily county scrape runs in its own thread, so emails, texts and agents keep running while it works.
    One scrape at a time; a scrape that fails is tried again after 30 minutes, not every loop pass."""
    t = state.get("thread")
    if t is not None and t.is_alive():
        return t
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or db.kv_get(con, "last_scrape_day") == today:
        return None
    if time.time() - state.get("failed_at", 0) < 1800:
        return None

    def work():
        wcon = db.connect()
        try:
            scrape.run(cfg)
            scrape.export()
            db.kv_set(wcon, "last_scrape_day", today)
        except Exception as e:
            state["failed_at"] = time.time()
            print(f"[{datetime.now():%H:%M}] scrape error (trying again in 30 min): {e}")
        finally:
            wcon.close()

    t = threading.Thread(target=work, name="daily-scrape", daemon=True)
    state["thread"] = t
    t.start()
    return t


def maybe_start_owner_lookup(cfg, con, now, state, hour):
    """Once a day, after the county scrape: look up owners for upcoming-sale leads (county appraiser records), in the background."""
    if not (cfg.get("owner_lookup") or {}).get("enabled", True):
        return None
    t = state.get("owners")
    if t is not None and t.is_alive():
        return t
    s = state.get("thread")
    if s is not None and s.is_alive():
        return None  # wait for the scrape to finish first
    today = now.strftime("%Y-%m-%d")
    if now.hour < hour or db.kv_get(con, "last_scrape_day") != today or db.kv_get(con, "owner_lookup_day") == today:
        return None
    db.kv_set(con, "owner_lookup_day", today)

    def work():
        from engine import owner_lookup
        wcon = db.connect()
        try:
            owner_lookup.enrich(wcon, limit=int((cfg.get("owner_lookup") or {}).get("per_day", 80)))
        except Exception as e:
            print(f"[{datetime.now():%H:%M}] owner lookup error: {e}")
        finally:
            wcon.close()

    t = threading.Thread(target=work, name="owner-lookup", daemon=True)
    state["owners"] = t
    t.start()
    return t


def maybe_start_clerk(cfg, con, now, state, hour):
    """Once a day after the scrape and owner lookup (or when the app asks): Pinellas Clerk pre-foreclosure, probate and
    eviction filings into The Hunter, in the background."""
    conf = cfg.get("clerk_records") or {}
    if not conf.get("enabled", True):
        return None
    t = state.get("clerk")
    if t is not None and t.is_alive():
        return t
    if any(state.get(k) is not None and state[k].is_alive() for k in ("thread", "owners")):
        return None  # one county job at a time
    today = now.strftime("%Y-%m-%d")
    try:
        asked = con.execute("""SELECT COUNT(*) FROM hunter_jobs j JOIN hunter_sources s ON s.id=j.source_id
                               WHERE j.status='queued' AND j.cancel=0 AND s.adapter='clerk_pinellas'""").fetchone()[0]
    except Exception:
        asked = 0
    due = now.hour >= hour and db.kv_get(con, "clerk_day") != today and time.time() - state.get("clerk_failed_at", 0) > 1800
    if not (due or asked):
        return None
    if due:
        db.kv_set(con, "clerk_day", today)

    def work():
        from engine import clerk_pinellas
        try:
            clerk_pinellas.sync(cfg)
        except Exception as e:
            state["clerk_failed_at"] = time.time()
            print(f"[{datetime.now():%H:%M}] clerk records error: {e}")

    t = threading.Thread(target=work, name="clerk-records", daemon=True)
    state["clerk"] = t
    t.start()
    return t


def maybe_start_precall(cfg, state):
    """Every 15 minutes: pre-call briefs for AI Sidekicks meetings in the next 36 hours and warm replies, in the background."""
    t = state.get("precall")
    if (t is not None and t.is_alive()) or time.time() - state.get("precall_at", 0) < 900:
        return None
    state["precall_at"] = time.time()

    def work():
        from engine import hq, precall
        qc = hq.connect()
        try:
            precall.tick(qc, cfg)
        except Exception as e:
            print(f"[{datetime.now():%H:%M}] pre-call brief error: {e}")
        try:  # AI Sidekicks CRM: read up to 10 new businesses' websites (free) so their scores rest on evidence
            from engine import dcrm
            n = dcrm.enrich(dcrm.connect(), 10)
            if n:
                print(f"[{datetime.now():%H:%M}] SDR: researched {n} business website(s) for the CRM")
        except Exception as e:
            print(f"[{datetime.now():%H:%M}] CRM research error: {e}")
        finally:
            qc.close()

    t = threading.Thread(target=work, name="precall", daemon=True)
    state["precall"] = t
    t.start()
    return t


def maybe_start_telegram(cfg, state):
    """The phone line (Telegram): answers Mola's messages and sends the team's updates. Runs only when the bot token is in .env."""
    import os
    t = state.get("telegram")
    if (t is not None and t.is_alive()) or not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or time.time() - state.get("telegram_at", 0) < 300:
        return None
    state["telegram_at"] = time.time()

    def work():
        from engine import telegram
        telegram.run_forever(cfg)

    t = threading.Thread(target=work, name="telegram", daemon=True)
    state["telegram"] = t
    t.start()
    return t


def maybe_start_voice_bridge(cfg, state):
    """Live voice calls: the bridge + Cloudflare tunnel, once the voice agents exist (python run.py voice-calls)."""
    t = state.get("voice_bridge_thread")
    if t is not None and t.is_alive():
        return None
    if time.time() - state.get("voice_bridge_at", 0) < 300:
        return None  # a failed start is retried every 5 minutes, not every loop
    from engine import voice_bridge
    if not voice_bridge.state().get("agents") or not voice_bridge.find_cloudflared():
        return None  # checked again every loop: starts by itself once setup and cloudflared are both done
    state["voice_bridge_at"] = time.time()
    t = threading.Thread(target=voice_bridge.run_forever, args=(cfg,), name="voice-calls", daemon=True)
    t.start()
    state["voice_bridge_thread"] = t
    return t


def loop(cfg):
    hour = int(cfg.get("scrape", {}).get("schedule_hour", 6))
    print(f"JARVIS Engine running. Scrape daily at {hour}:00, campaign texts every 2 min, replies every minute, feed every 5. Ctrl+C to stop.")
    con = db.connect()
    last = {"sms": 0, "poll": 0, "feed": 0}
    from engine import agent
    try:
        agent.backfill(con)
    except Exception as e:
        print(f"(inbox backfill skipped: {e})")
    from engine import hunter
    try:
        hcon = hunter.connect()
        hunter.sync_registry(hcon, cfg)
        n = hunter.backfill_legacy(hcon, cfg)
        if n:
            print(f"Hunter: loaded {n} existing county records.")
    except Exception as e:
        hcon = None
        print(f"(Hunter setup skipped: {e})")
    from engine import hq
    try:
        qcon = hq.connect()
        n = hq.recover(qcon)
        if n:
            print(f"HQ: recovered {n} interrupted task(s).")
    except Exception as e:
        qcon = None
        print(f"(HQ setup skipped: {e})")
    poll_every = int(cfg.get("sms", {}).get("poll_seconds", 60))
    scrape_state = {}
    while True:
        now = datetime.now()
        try:
            maybe_start_scrape(cfg, con, now, scrape_state, hour)
            maybe_start_owner_lookup(cfg, con, now, scrape_state, hour)
            maybe_start_clerk(cfg, con, now, scrape_state, hour)
            maybe_start_precall(cfg, scrape_state)
            maybe_start_telegram(cfg, scrape_state)
            maybe_start_voice_bridge(cfg, scrape_state)
            if db.kv_get(con, "leads_expired_day") != now.strftime("%Y-%m-%d"):  # once a day: past auctions leave the review list
                n = db.expire_past(con)
                db.kv_set(con, "leads_expired_day", now.strftime("%Y-%m-%d"))
                if n:
                    print(f"[{now:%H:%M}] {n} county lead(s) set aside: their sale date has passed.")
            t = time.time()
            if t - last["poll"] > poll_every:
                sms.poll(cfg); last["poll"] = t
            agent.process_outbox(con, cfg)
            if hcon is not None:
                hunter.run_queued(hcon, cfg)
            if qcon is not None:
                hq.tick(qcon, cfg)
                db.kv_set(qcon, "hq_last_tick", datetime.now().isoformat(timespec="seconds"))
                try:  # AI Sidekicks email from the PC: approved campaign emails (paced) and replies, when Gmail is connected
                    hq.engine_send_tick(qcon, cfg)
                    hq.engine_reply_tick(qcon, cfg)
                except Exception as e:
                    print(f"[{now:%H:%M}] email: {e}")
            if t - last["sms"] > 120:
                sms.tick(cfg); last["sms"] = t
            if t - last["feed"] > 300:
                feed.process(cfg); last["feed"] = t
        except Exception as e:
            print(f"[{now:%H:%M}] error: {e}")
        # sleep up to 30 s, but wake early when the app asks for work to run now
        seen = db.kv_get(con, "hq_run_now") or ""
        for _ in range(15):
            time.sleep(2)
            try:
                if (db.kv_get(con, "hq_run_now") or "") != seen:
                    break
            except Exception:
                pass


if __name__ == "__main__":
    main()
