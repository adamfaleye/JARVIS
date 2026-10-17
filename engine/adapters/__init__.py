"""Adapter registry. Each adapter exposes fetch(county: dict, cfg: dict, log) -> list[dict]."""
import importlib


def get(name):
    name = name or "ai_page"
    mod = name.split(":", 1)[1] if name.startswith("custom:") else name
    return importlib.import_module(f"engine.adapters.{mod}")
