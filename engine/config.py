"""Loads config.yaml, counties.yaml and .env."""
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load():
    _load_env()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    cfg["counties"] = load_counties()
    return cfg


def load_counties():
    p = ROOT / "counties.yaml"
    if not p.exists():
        return []
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("counties", []) or []


def save_counties(counties):
    p = ROOT / "counties.yaml"
    header = ("# One entry per county source. Adapters: realauction, ai_page, csv_url, custom:<module>.\n"
              "# Add with: python run.py add-county \"Pasco\" --state FL\n")
    p.write_text(header + yaml.safe_dump({"counties": counties}, sort_keys=False), encoding="utf-8")


def path(rel):
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p
