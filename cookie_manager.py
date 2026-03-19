"""
YouTube cookie auto-refresh module.

Keeps YouTube cookies alive by periodically loading them into a headless
Chromium browser and saving the rotated values back.  YouTube rotates cookie
values as a security measure; if the server holds stale values yt-dlp gets
"Sign in to confirm you're not a bot".  By replaying the cookies in a real
browser session we capture the fresh values automatically.

Flow
----
1. On first start, seed from the mounted cookie file (exported from browser).
2. Background thread refreshes every N hours (default 4).
3. If a download fails with bot-detection, an immediate refresh is triggered.
"""

import os
import time
import logging
import threading
import shutil

logger = logging.getLogger(__name__)

SEED_FILE = os.environ.get("YOUTUBE_COOKIES_SEED", "/app/youtube_cookies_seed.txt")
COOKIE_FILE = os.environ.get("YOUTUBE_COOKIES_FILE", "/app/cookie_data/youtube_cookies.txt")
REFRESH_HOURS = int(os.environ.get("COOKIE_REFRESH_HOURS", "4"))

_refresh_lock = threading.Lock()


# ── Netscape cookie helpers ──────────────────────────────────────────

def _parse_netscape(path):
    """Parse a Netscape-format cookie file into Selenium-style dicts."""
    cookies = []
    if not path or not os.path.exists(path):
        return cookies
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            cookie = {
                "domain": parts[0],
                "path": parts[2],
                "secure": parts[3] == "TRUE",
                "name": parts[5],
                "value": parts[6],
            }
            if parts[4] and parts[4] != "0":
                cookie["expiry"] = int(parts[4])
            cookies.append(cookie)
    return cookies


def _write_netscape(cookies, path):
    """Write Selenium cookie list to Netscape cookie file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Netscape HTTP Cookie File",
        "# Auto-refreshed by cookie_manager",
        "# This is a generated file! Do not edit.",
        "",
    ]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        c_path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = str(int(c.get("expiry", 0)))
        lines.append(f"{domain}\t{flag}\t{c_path}\t{secure}\t{expiry}\t{c['name']}\t{c['value']}")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# ── Chromium driver ──────────────────────────────────────────────────

def _make_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    opts.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=opts)


# ── Public API ───────────────────────────────────────────────────────

def seed_cookies():
    """Copy seed file → working file on first run."""
    if os.path.exists(COOKIE_FILE):
        logger.info("Working cookie file already exists, skipping seed")
        return
    if os.path.exists(SEED_FILE):
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        shutil.copy2(SEED_FILE, COOKIE_FILE)
        logger.info("Seeded YouTube cookies from %s", SEED_FILE)
    else:
        logger.warning("No seed cookie file found at %s", SEED_FILE)


def refresh_cookies():
    """Open YouTube in headless Chromium, capture rotated cookies."""
    if not _refresh_lock.acquire(blocking=False):
        logger.info("Cookie refresh already in progress, skipping")
        return False

    try:
        source = COOKIE_FILE if os.path.exists(COOKIE_FILE) else SEED_FILE
        existing = _parse_netscape(source)
        if not existing:
            logger.warning("No YouTube cookies available to refresh")
            return False

        logger.info("Refreshing YouTube cookies (%d cookies from %s)", len(existing), source)
        driver = _make_driver()
        try:
            # Visit YouTube to set domain
            driver.get("https://www.youtube.com")
            time.sleep(2)

            # Inject our cookies
            driver.delete_all_cookies()
            for cookie in existing:
                try:
                    driver.add_cookie(cookie)
                except Exception as exc:
                    logger.debug("Skip cookie %s: %s", cookie.get("name"), exc)

            # Reload — YouTube rotates cookie values in the response
            driver.get("https://www.youtube.com")
            time.sleep(5)

            # Save refreshed cookies
            new_cookies = driver.get_cookies()
            if new_cookies:
                _write_netscape(new_cookies, COOKIE_FILE)
                logger.info("Saved %d refreshed YouTube cookies", len(new_cookies))
                return True
            else:
                logger.warning("No cookies returned after refresh")
                return False
        finally:
            driver.quit()
    except Exception as exc:
        logger.error("Cookie refresh failed: %s", exc)
        return False
    finally:
        _refresh_lock.release()


def is_bot_detection_error(error):
    """Check if an exception is a YouTube bot-detection error."""
    msg = str(error).lower()
    return "sign in" in msg and "bot" in msg


# ── Background daemon ────────────────────────────────────────────────

class CookieRefreshDaemon(threading.Thread):
    """Periodically refreshes YouTube cookies in the background."""

    def __init__(self, interval_hours=REFRESH_HOURS):
        super().__init__(daemon=True, name="yt-cookie-refresh")
        self.interval = interval_hours * 3600
        self._stop = threading.Event()

    def run(self):
        # Initial refresh shortly after startup
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                refresh_cookies()
            except Exception as exc:
                logger.error("Cookie daemon error: %s", exc)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
