"""WhatsApp deal-feed processor. Drop chat exports (.txt) in inbox/feed; each property is extracted,
deduped, and matched against data/buyers.csv. Results: out/feed_matches.csv and out/jarvis_feed_import.csv."""
import csv
import json
import re
import shutil
from . import db, config, ai

FIELDS = {"address": "property street address with city/state if given", "county": "county if stated",
          "asking": "asking price", "arv": "ARV", "beds": "beds", "baths": "baths", "sqft": "sqft",
          "condition": "condition / repairs", "notes": "anything else useful incl. who posted it and contact"}


def extract(text, model):
    items = []
    for i in range(0, len(text), 12000):
        chunk = text[i:i + 12000]
        out = ai.extract_json(f"""This is a WhatsApp chat export from an off-market real estate deal group.
Extract every property being offered. Fields: {json.dumps(FIELDS)}.
Rules: copy values exactly as written; numbers as plain digits; omit fields not stated; never invent an address,
price, or ARV; one entry per property; ignore chit-chat. If a Florida city is given and the county is unambiguous, fill county.
Return ONLY JSON: {{"items": [...]}}

CHAT:
{chunk}""", model) or {}
        items += [x for x in out.get("items", []) if isinstance(x, dict) and x.get("address")]
    return items


def load_buyers(cfg):
    p = config.path(cfg["feed"].get("buyers_csv", "data/buyers.csv"))
    if not p.exists():
        return []
    buyers = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            low = {k.lower().strip(): (v or "").strip() for k, v in r.items() if k}
            pick = lambda *ks: next((low[k] for k in low if any(x in k for x in ks) and low[k]), "")
            buyers.append({"name": pick("name", "company"), "phone": pick("phone", "mobile", "cell"), "email": pick("email"),
                           "areas": pick("area", "market", "count", "cit", "zip", "location"),
                           "max_price": db.money(pick("max", "budget", "price")) or 0})
    return buyers


def match(deal, buyers):
    hay = f"{deal.get('address','')} {deal.get('county','')}".lower()
    price = db.money(deal.get("asking")) or 0
    hits = []
    for b in buyers:
        areas = [a.strip().lower() for a in re.split(r"[,;/|]+", b["areas"]) if a.strip()]
        area_ok = not areas or any(a in hay for a in areas)
        price_ok = not b["max_price"] or not price or price <= b["max_price"]
        if area_ok and price_ok:
            hits.append(b)
    return hits


def process(cfg, log=print):
    inbox = config.path(cfg["feed"].get("inbox", "inbox/feed"))
    inbox.mkdir(parents=True, exist_ok=True)
    done_dir = inbox / "processed"
    done_dir.mkdir(exist_ok=True)
    files = sorted(inbox.glob("*.txt"))
    if not files:
        return 0
    con, buyers = db.connect(), load_buyers(cfg)
    new_deals = []
    for fp in files:
        log(f"Reading {fp.name}…")
        items = extract(fp.read_text(encoding="utf-8", errors="ignore"), cfg["anthropic_model"])
        for d in items:
            key = db.norm_addr(d["address"])
            hits = match(d, buyers)
            cur = con.execute("""INSERT OR IGNORE INTO feed_deals(key,address,county,asking,arv,beds,baths,sqft,condition,notes,source_file,first_seen,matches)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (key, d.get("address"), d.get("county"), db.money(d.get("asking")), db.money(d.get("arv")),
                               d.get("beds"), d.get("baths"), d.get("sqft"), d.get("condition"), d.get("notes"), fp.name,
                               db.now(), json.dumps([h["name"] for h in hits])))
            if cur.rowcount:
                new_deals.append((d, hits))
        con.commit()
        shutil.move(str(fp), str(done_dir / fp.name))
    out = config.path("out")
    out.mkdir(exist_ok=True)
    mp, jp = out / "feed_matches.csv", out / f"jarvis_feed_import_{db.now()[:10]}.csv"
    new_m = not mp.exists()
    with open(mp, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_m:
            w.writerow(["Found", "Address", "County", "Asking", "ARV", "Bed/Bath", "Sqft", "Condition", "Matched buyers", "Buyer contacts"])
        for d, hits in new_deals:
            w.writerow([db.now(), d.get("address"), d.get("county", ""), d.get("asking", ""), d.get("arv", ""),
                        f"{d.get('beds','')}/{d.get('baths','')}", d.get("sqft", ""), d.get("condition", ""),
                        "; ".join(h["name"] for h in hits), "; ".join(h["email"] or h["phone"] for h in hits)])
    with open(jp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Property Address", "County", "Asking", "ARV", "Beds", "Baths", "Sqft", "Condition", "Notes"])
        for d, _ in new_deals:
            w.writerow([d.get("address"), d.get("county", ""), d.get("asking", ""), d.get("arv", ""), d.get("beds", ""),
                        d.get("baths", ""), d.get("sqft", ""), d.get("condition", ""), d.get("notes", "")])
    log(f"{len(new_deals)} new feed deals, {sum(1 for _, h in new_deals if h)} with buyer matches → {mp.name}")
    return len(new_deals)
