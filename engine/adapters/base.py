"""Shared browser + helpers for adapters."""
import contextlib
import time


class Blocked(Exception):
    """The site is showing a CAPTCHA or blocking automated access. We stop rather than bypass it."""


@contextlib.contextmanager
def browser(cfg):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        headless = cfg.get("scrape", {}).get("headless", True)
        try:
            b = p.chromium.launch(headless=headless)
        except Exception:
            # Playwright's own Chromium isn't installed; use Microsoft Edge, which every Windows PC has.
            b = p.chromium.launch(channel="msedge", headless=headless)
        ctx = b.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()
            b.close()


def check_blocked(page):
    html = page.content().lower()
    if "captcha" in html or "verify you are human" in html or "access denied" in html[:5000]:
        raise Blocked(f"{page.url} is showing a CAPTCHA/block page. Skipping; not bypassing it.")


def pause(cfg):
    time.sleep(float(cfg.get("scrape", {}).get("delay_seconds", 3)))
