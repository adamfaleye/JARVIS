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
    wp = sub.add_parser("web"); wp.add_argument("--port", type=int, default=8443); wp.add_argument("--local", action="store_true")
    wp.add_argument("--http", action="store_true"); wp.add_argument("--pair", action="store_true"); wp.add_argument("--revoke", action="store_true")
    vp = sub.add_parser("voice"); vp.add_argument("--device", type=int); vp.add_argument("--list-devices", action="store_true")
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
    elif args.cmd == "loop":
        loop(cfg)
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
    elif args.cmd == "voice":
        from voice import jarvis_voice
        jarvis_voice.list_devices() if args.list_devices else jarvis_voice.main(args.device)


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
    while True:
        now = datetime.now()
        try:
            if now.hour >= hour and db.kv_get(con, "last_scrape_day") != now.strftime("%Y-%m-%d"):
                scrape.run(cfg)
                scrape.export()
                db.kv_set(con, "last_scrape_day", now.strftime("%Y-%m-%d"))
            t = time.time()
            if t - last["poll"] > poll_every:
                sms.poll(cfg); last["poll"] = t
            agent.process_outbox(con, cfg)
            if hcon is not None:
                hunter.run_queued(hcon, cfg)
            if qcon is not None:
                hq.tick(qcon, cfg)
                db.kv_set(qcon, "hq_last_tick", datetime.now().isoformat(timespec="seconds"))
            if t - last["sms"] > 120:
                sms.tick(cfg); last["sms"] = t
            if t - last["feed"] > 300:
                feed.process(cfg); last["feed"] = t
        except Exception as e:
            print(f"[{now:%H:%M}] error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()
