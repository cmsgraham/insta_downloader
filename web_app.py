"""
Instagram Downloader — Public Web Service
Server uses the owner's cookies.txt for all requests.
Visitors just paste a URL and hit Download.
"""

import os
import re
import json
import string
import time
import secrets
import requests
import threading
import uuid
import hmac
from pathlib import Path

from flask import Flask, render_template_string, request, jsonify, session, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL = int(os.environ.get("FILE_TTL_SECONDS", "3600"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# ──────────────────────────────────────────────
#  Server-wide Instagram session (from cookies.txt)
# ──────────────────────────────────────────────

_server_session = {
    "ig_sessionid": None,
    "ig_cookies": {},
    "rate_limit_until": 0,
}
_session_lock = threading.Lock()


def _load_cookies_file():
    cookie_path = os.environ.get("COOKIES_FILE", "/app/cookies.txt")
    if not os.path.exists(cookie_path):
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
        if os.path.exists(local):
            cookie_path = local
        else:
            print("[!] No cookies.txt found.")
            return

    cookies = {}
    with open(cookie_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name, value = parts[5], parts[6]
                cookies[name] = value

    with _session_lock:
        _server_session["ig_sessionid"] = cookies.get("sessionid")
        _server_session["ig_cookies"] = {k: v for k, v in cookies.items() if k != "sessionid"}

    if _server_session["ig_sessionid"]:
        print(f"[+] Loaded server session (ds_user_id={cookies.get('ds_user_id', '?')})")
    else:
        print("[!] cookies.txt found but no sessionid.")


_load_cookies_file()

# Per-visitor rate limiting
VISITOR_MAX_DOWNLOADS_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "10"))
VISITOR_MAX_CONCURRENT = 3

# Per-visitor state: uid -> { jobs, download_dir, request_times }
_visitors = {}
_visitors_lock = threading.Lock()


def _get_visitor():
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
    uid = session["uid"]
    with _visitors_lock:
        if uid not in _visitors:
            visitor_dir = os.path.join(DOWNLOAD_DIR, uid)
            os.makedirs(visitor_dir, exist_ok=True)
            _visitors[uid] = {
                "jobs": {},
                "download_dir": visitor_dir,
                "request_times": [],
            }
        return _visitors[uid]


def _check_visitor_rate_limit(visitor):
    """Returns error message if rate-limited, None if OK."""
    now = time.time()
    # Clean old timestamps
    visitor["request_times"] = [t for t in visitor["request_times"] if now - t < 60]
    # Check per-minute limit
    if len(visitor["request_times"]) >= VISITOR_MAX_DOWNLOADS_PER_MIN:
        return f"Too many requests. Max {VISITOR_MAX_DOWNLOADS_PER_MIN} downloads per minute."
    # Check concurrent jobs
    active = sum(1 for j in visitor["jobs"].values() if j["status"] == "working")
    if active >= VISITOR_MAX_CONCURRENT:
        return f"Too many downloads in progress. Wait for current ones to finish."
    return None


# ──────────────────────────────────────────────
#  Instagram helpers
# ──────────────────────────────────────────────

IG_APP_ID = "936619743392459"
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": IG_APP_ID,
    "X-Requested-With": "XMLHttpRequest",
}


def _make_ig_session():
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    with _session_lock:
        if _server_session["ig_sessionid"]:
            s.cookies.set("sessionid", _server_session["ig_sessionid"], domain=".instagram.com")
        for k, v in _server_session["ig_cookies"].items():
            if v:
                s.cookies.set(k, v, domain=".instagram.com")
    return s


def shortcode_to_media_id(shortcode):
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    media_id = 0
    for c in shortcode:
        media_id = media_id * 64 + alphabet.index(c)
    return media_id


def parse_url(url):
    url = url.strip().rstrip("/")
    m = re.match(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if m:
        return ("post", m.group(1))
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/(\d+)", url)
    if m:
        return ("story", (m.group(1), m.group(2)))
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))
    m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))
    return (None, None)


def _check_rate_limit():
    remaining = _server_session["rate_limit_until"] - time.time()
    if remaining > 0:
        raise ConnectionAbortedError(
            f"Instagram rate-limited. Try again in {int(remaining)+1}s."
        )


def _record_rate_limit(retry_after=None):
    cooldown = int(retry_after) if retry_after else 60
    _server_session["rate_limit_until"] = time.time() + cooldown


def _fetch_media_v1(media_id):
    _check_rate_limit()
    s = _make_ig_session()
    r = s.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/", timeout=20)
    if r.status_code == 429:
        _record_rate_limit(r.headers.get("Retry-After"))
        raise ConnectionAbortedError("v1 API rate-limited (429)")
    if r.status_code != 200:
        raise RuntimeError(f"v1 API returned status {r.status_code}")
    if "application/json" not in r.headers.get("content-type", ""):
        raise RuntimeError("Instagram returned HTML — session may be expired.")
    return r.json()


def _fetch_media_graphql(shortcode):
    _check_rate_limit()
    s = _make_ig_session()
    page = s.get(f"https://www.instagram.com/p/{shortcode}/", timeout=20)
    if page.status_code == 429:
        _record_rate_limit(page.headers.get("Retry-After"))
        raise ConnectionAbortedError("GraphQL page load rate-limited (429)")
    if page.status_code != 200:
        raise RuntimeError(f"Could not load post page (status {page.status_code})")
    dtsg_match = re.search(r'"DTSGInitialData".*?"token"\s*:\s*"([^"]+)"', page.text)
    if not dtsg_match:
        raise RuntimeError("Could not extract fb_dtsg token from page")
    fb_dtsg = dtsg_match.group(1)
    csrf = s.cookies.get("csrftoken", domain=".instagram.com") or ""
    r = s.post(
        "https://www.instagram.com/graphql/query",
        data={
            "fb_dtsg": fb_dtsg,
            "doc_id": "8845758582119845",
            "variables": json.dumps({"shortcode": shortcode}),
        },
        headers={
            "X-FB-Friendly-Name": "PolarisPostActionLoadPostQueryQuery",
            "X-CSRFToken": csrf,
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        },
        timeout=20,
    )
    if r.status_code == 429:
        _record_rate_limit(r.headers.get("Retry-After"))
        raise ConnectionAbortedError("GraphQL rate-limited (429)")
    if r.status_code != 200 or not r.text:
        raise RuntimeError(f"GraphQL returned status {r.status_code}")
    if "application/json" not in r.headers.get("content-type", ""):
        raise RuntimeError("GraphQL returned HTML — session may be invalid.")
    data = r.json()
    media = data.get("data", {}).get("xdt_shortcode_media")
    if not media:
        raise RuntimeError("GraphQL returned null — session may be expired.")
    item = {"code": shortcode}
    if media.get("is_video") and media.get("video_url"):
        item["media_type"] = 2
        item["video_versions"] = [{"url": media["video_url"], "width": 0, "height": 0}]
    elif media.get("edge_sidecar_to_children"):
        item["media_type"] = 8
        item["carousel_media"] = []
        for edge in media["edge_sidecar_to_children"].get("edges", []):
            node = edge.get("node", {})
            sub = {}
            if node.get("is_video") and node.get("video_url"):
                sub["media_type"] = 2
                sub["video_versions"] = [{"url": node["video_url"], "width": 0, "height": 0}]
            elif node.get("display_url"):
                sub["media_type"] = 1
                sub["image_versions2"] = {"candidates": [{"url": node["display_url"], "width": 0, "height": 0}]}
            item["carousel_media"].append(sub)
    elif media.get("display_url"):
        item["media_type"] = 1
        item["image_versions2"] = {"candidates": [{"url": media["display_url"], "width": 0, "height": 0}]}
    return {"items": [item]}


def fetch_media_info(media_id, shortcode=None):
    v1_err = None
    gql_err = None
    delays = [0, 5, 15]
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            return _fetch_media_v1(media_id)
        except ConnectionAbortedError as e:
            v1_err = e
            continue
        except RuntimeError as e:
            v1_err = e
            break
    if shortcode:
        try:
            return _fetch_media_graphql(shortcode)
        except Exception as e:
            gql_err = e
    remaining = _server_session["rate_limit_until"] - time.time()
    if remaining > 0:
        raise ConnectionAbortedError(f"Instagram rate-limited. Try again in ~{int(remaining)+1}s.")
    parts = ["Both v1 API and GraphQL failed."]
    if v1_err:
        parts.append(f"v1: {v1_err}")
    if gql_err:
        parts.append(f"GraphQL: {gql_err}")
    parts.append("Wait a minute and try again.")
    raise RuntimeError(" ".join(parts))


def _fetch_user_id_v1(username):
    s = _make_ig_session()
    r = s.get(f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}", timeout=20)
    if r.status_code == 429:
        raise ConnectionAbortedError("v1 user lookup rate-limited (429)")
    if r.status_code != 200:
        raise RuntimeError(f"v1 user lookup returned status {r.status_code}")
    if "application/json" not in r.headers.get("content-type", ""):
        raise RuntimeError("v1 user lookup returned HTML — session may be expired.")
    data = r.json()
    return data["data"]["user"]["id"]


def _fetch_user_id_search(username):
    s = _make_ig_session()
    r = s.get("https://www.instagram.com/web/search/topsearch/", params={"query": username}, timeout=20)
    if r.status_code == 429:
        raise ConnectionAbortedError("Search API also rate-limited (429)")
    if r.status_code != 200:
        raise RuntimeError(f"Search API returned status {r.status_code}")
    data = r.json()
    for entry in data.get("users", []):
        u = entry.get("user", {})
        if u.get("username", "").lower() == username.lower():
            return str(u["pk"])
    raise RuntimeError(f"User '{username}' not found in search results.")


def fetch_user_id(username):
    try:
        uid = _fetch_user_id_v1(username)
        print(f"[+] Resolved @{username} via v1 API -> {uid}")
        return uid
    except (ConnectionAbortedError, RuntimeError) as e:
        print(f"[!] v1 user lookup failed: {e} — trying search fallback")
    try:
        uid = _fetch_user_id_search(username)
        print(f"[+] Resolved @{username} via search API -> {uid}")
        return uid
    except ConnectionAbortedError:
        _record_rate_limit()
        raise ConnectionAbortedError("Instagram rate-limited on all endpoints. Try again in ~60s.")
    except RuntimeError as e:
        raise RuntimeError(f"Could not resolve user '{username}': {e}")


def fetch_stories(ig_user_id):
    s = _make_ig_session()
    r = s.get(f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={ig_user_id}", timeout=20)
    if r.status_code == 429:
        _record_rate_limit(r.headers.get("Retry-After"))
        raise ConnectionAbortedError("Instagram rate-limited. Try again in ~60s.")
    if r.status_code != 200:
        raise RuntimeError(f"Could not fetch stories (status {r.status_code})")
    data = r.json()
    reels = data.get("reels", {}) or data.get("reels_media", [])
    if isinstance(reels, dict):
        reel = reels.get(str(ig_user_id), {})
        return reel.get("items", [])
    elif isinstance(reels, list):
        for reel in reels:
            if str(reel.get("id", "")) == str(ig_user_id):
                return reel.get("items", [])
    return []


def download_file(url, filepath):
    s = _make_ig_session()
    r = s.get(url, timeout=60, stream=True)
    r.raise_for_status()
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return os.path.getsize(filepath)


def pick_best_video(video_versions):
    if not video_versions:
        return None
    return max(video_versions, key=lambda v: v.get("width", 0) * v.get("height", 0))


def download_media_item(item, dl_dir, prefix=""):
    files = []
    media_type = item.get("media_type")
    code = item.get("code", prefix or "unknown")
    if media_type == 2 and "video_versions" in item:
        best = pick_best_video(item["video_versions"])
        if best:
            fname = f"{code}.mp4"
            download_file(best["url"], os.path.join(dl_dir, fname))
            files.append(fname)
    elif media_type == 8 and "carousel_media" in item:
        for i, sub in enumerate(item["carousel_media"]):
            sub_code = f"{code}_slide{i+1}"
            if sub.get("media_type") == 2 and "video_versions" in sub:
                best = pick_best_video(sub["video_versions"])
                if best:
                    fname = f"{sub_code}.mp4"
                    download_file(best["url"], os.path.join(dl_dir, fname))
                    files.append(fname)
            elif "image_versions2" in sub:
                candidates = sub["image_versions2"].get("candidates", [])
                if candidates:
                    best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
                    fname = f"{sub_code}.jpg"
                    download_file(best_img["url"], os.path.join(dl_dir, fname))
                    files.append(fname)
    elif media_type == 1 and "image_versions2" in item:
        candidates = item["image_versions2"].get("candidates", [])
        if candidates:
            best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            fname = f"{code}.jpg"
            download_file(best_img["url"], os.path.join(dl_dir, fname))
            files.append(fname)
    return files


def download_story_item(item, dl_dir, username="story"):
    files = []
    story_id = item.get("pk", item.get("id", "unknown"))
    if item.get("media_type") == 2 and "video_versions" in item:
        best = pick_best_video(item["video_versions"])
        if best:
            fname = f"{username}_story_{story_id}.mp4"
            download_file(best["url"], os.path.join(dl_dir, fname))
            files.append(fname)
    elif "image_versions2" in item:
        candidates = item["image_versions2"].get("candidates", [])
        if candidates:
            best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            fname = f"{username}_story_{story_id}.jpg"
            download_file(best_img["url"], os.path.join(dl_dir, fname))
            files.append(fname)
    return files


# ──────────────────────────────────────────────
#  Background download worker
# ──────────────────────────────────────────────

def run_download(job_id, url, visitor):
    try:
        content_type, identifier = parse_url(url)
        if content_type is None:
            visitor["jobs"][job_id] = {"status": "error", "message": f"Could not parse URL: {url}", "files": []}
            return

        if not _server_session["ig_sessionid"]:
            visitor["jobs"][job_id] = {"status": "error", "message": "Service temporarily unavailable.", "files": []}
            return

        dl_dir = visitor["download_dir"]
        all_files = []

        if content_type == "post":
            shortcode = identifier
            media_id = shortcode_to_media_id(shortcode)
            data = fetch_media_info(media_id, shortcode=shortcode)
            if not data.get("items"):
                visitor["jobs"][job_id] = {"status": "error", "message": "Media not found or unavailable.", "files": []}
                return
            item = data["items"][0]
            all_files = download_media_item(item, dl_dir, prefix=shortcode)

        elif content_type == "story":
            username, story_pk = identifier
            ig_user_id = fetch_user_id(username)
            items = fetch_stories(ig_user_id)
            found = False
            for item in items:
                if str(item.get("pk")) == story_pk or str(item.get("id")) == story_pk:
                    all_files = download_story_item(item, dl_dir, username)
                    found = True
                    break
            if not found:
                visitor["jobs"][job_id] = {"status": "error", "message": f"Story {story_pk} not found. It may have expired.", "files": []}
                return

        elif content_type == "profile_stories":
            username = identifier
            ig_user_id = fetch_user_id(username)
            items = fetch_stories(ig_user_id)
            if not items:
                visitor["jobs"][job_id] = {"status": "error", "message": f"No active stories found for @{username}.", "files": []}
                return
            for item in items:
                all_files.extend(download_story_item(item, dl_dir, username))

        if all_files:
            visitor["jobs"][job_id] = {"status": "done", "message": f"Downloaded {len(all_files)} file(s).", "files": all_files}
        else:
            visitor["jobs"][job_id] = {"status": "done", "message": "No downloadable media found.", "files": []}

    except ConnectionAbortedError as e:
        visitor["jobs"][job_id] = {"status": "error", "message": str(e), "files": []}
    except Exception as e:
        visitor["jobs"][job_id] = {"status": "error", "message": str(e), "files": []}


# ──────────────────────────────────────────────
#  Periodic cleanup
# ──────────────────────────────────────────────

def _cleanup_loop():
    while True:
        time.sleep(300)
        try:
            now = time.time()
            for uid_dir in Path(DOWNLOAD_DIR).iterdir():
                if not uid_dir.is_dir():
                    continue
                for f in uid_dir.iterdir():
                    if f.is_file() and (now - f.stat().st_mtime) > FILE_TTL:
                        f.unlink(missing_ok=True)
                if uid_dir.is_dir() and not any(uid_dir.iterdir()):
                    uid_dir.rmdir()
        except Exception:
            pass

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    _get_visitor()
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/download", methods=["POST"])
def api_download():
    visitor = _get_visitor()
    # Per-visitor rate limit
    err = _check_visitor_rate_limit(visitor)
    if err:
        return jsonify({"error": err}), 429
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    visitor["request_times"].append(time.time())
    job_id = uuid.uuid4().hex[:12]
    visitor["jobs"][job_id] = {"status": "working", "message": "Downloading...", "files": []}
    thread = threading.Thread(target=run_download, args=(job_id, url, visitor), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    visitor = _get_visitor()
    job = visitor["jobs"].get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/health")
def api_health():
    remaining = max(0, int(_server_session["rate_limit_until"] - time.time()))
    return jsonify({
        "session_active": _server_session["ig_sessionid"] is not None,
        "cooldown": remaining,
    })


@app.route("/api/admin/reload-cookies", methods=["POST"])
def api_admin_reload():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    _load_cookies_file()
    return jsonify({"ok": True, "session_active": _server_session["ig_sessionid"] is not None})


@app.route("/downloads/<path:filename>")
def serve_file(filename):
    visitor = _get_visitor()
    return send_from_directory(visitor["download_dir"], filename, as_attachment=True)


@app.route("/robots.txt")
def robots_txt():
    return app.response_class(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /downloads/\n\nSitemap: https://freeinsta.website/sitemap.xml\n",
        mimetype="text/plain",
    )


@app.route("/sitemap.xml")
def sitemap_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://freeinsta.website/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return app.response_class(xml, mimetype="application/xml")


# ──────────────────────────────────────────────
#  HTML Template
# ──────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1">
<title>FreeInsta — Download Instagram Reels, Posts & Stories Free</title>
<meta name="description" content="Download Instagram reels, posts, stories and photos for free. No login required. Just paste the link and download instantly.">
<meta name="keywords" content="instagram downloader, download instagram reels, download instagram stories, save instagram posts, instagram video downloader, free instagram downloader">
<link rel="canonical" href="https://freeinsta.website/">

<!-- Open Graph -->
<meta property="og:title" content="FreeInsta — Free Instagram Downloader">
<meta property="og:description" content="Download Instagram reels, posts & stories for free. No login required.">
<meta property="og:url" content="https://freeinsta.website/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="FreeInsta">

<!-- Twitter Card -->
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="FreeInsta — Free Instagram Downloader">
<meta name="twitter:description" content="Download Instagram reels, posts & stories for free. No login required.">
<style>
    :root {
        --bg: #0a0a0a;
        --card: #161616;
        --border: #2a2a2a;
        --accent: #e1306c;
        --accent2: #833ab4;
        --text: #f5f5f5;
        --muted: #888;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: var(--bg);
        color: var(--text);
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 16px;
    }
    .container { width: 100%; max-width: 520px; }
    .logo { text-align: center; margin-bottom: 32px; }
    .logo h1 {
        font-size: 28px;
        background: linear-gradient(45deg, var(--accent), var(--accent2));
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .logo p { color: var(--muted); font-size: 14px; margin-top: 8px; }
    .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 24px;
    }
    .field { margin-bottom: 18px; }
    .field label {
        display: block; font-size: 13px; color: var(--muted); margin-bottom: 8px;
        text-transform: uppercase; letter-spacing: 0.5px;
    }
    .field input {
        width: 100%; padding: 16px; background: var(--bg); border: 1px solid var(--border);
        border-radius: 10px; color: var(--text); font-size: 16px; outline: none;
        transition: border-color 0.2s; font-family: inherit;
    }
    .field input:focus { border-color: var(--accent); }
    .field input::placeholder { color: #555; }
    .btn {
        width: 100%; padding: 16px;
        background: linear-gradient(45deg, var(--accent), var(--accent2));
        color: #fff; border: none; border-radius: 10px; font-size: 16px; font-weight: 600;
        cursor: pointer; transition: opacity 0.2s; -webkit-tap-highlight-color: transparent;
    }
    .btn:hover { opacity: 0.9; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .status { margin-top: 16px; padding: 14px; border-radius: 10px; font-size: 14px; display: none; }
    .status.show { display: block; }
    .status.working { background: #1a1a2e; border: 1px solid #333; }
    .status.done { background: #0a2e1a; border: 1px solid #1a5e3a; }
    .status.error { background: #2e0a0a; border: 1px solid #5e1a1a; }
    .file-list { margin-top: 10px; }
    .file-list a {
        display: block; color: var(--accent); text-decoration: none;
        padding: 8px 0; font-size: 15px;
    }
    .file-list a:hover { text-decoration: underline; }
    .spinner {
        display: inline-block; width: 16px; height: 16px;
        border: 2px solid var(--muted); border-top-color: var(--accent);
        border-radius: 50%; animation: spin 0.8s linear infinite;
        vertical-align: middle; margin-right: 8px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .footer {
        text-align: center; margin-top: 20px; font-size: 12px;
        color: var(--muted); line-height: 1.6;
    }
    .types {
        display: flex; justify-content: center; gap: 16px;
        margin-top: 12px; font-size: 13px; color: var(--muted);
    }
    .types span { display: flex; align-items: center; gap: 4px; }
</style>
</head>
<body>
<div class="container">
    <div class="logo">
        <h1>FreeInsta</h1>
        <p>Download reels, posts &amp; stories from Instagram</p>
    </div>

    <div class="card">
        <div class="field">
            <label>Instagram URL</label>
            <input type="url" id="url" placeholder="Paste link here..." autofocus>
        </div>
        <button class="btn" id="downloadBtn" onclick="startDownload()">Download</button>
        <div class="status" id="status"></div>
    </div>

    <div class="types">
        <span>&#127910; Reels</span>
        <span>&#128247; Posts</span>
        <span>&#128248; Stories</span>
    </div>

    <div class="footer">
        Free &amp; private &mdash; no login required<br>
        Downloads auto-expire after 1 hour
    </div>
</div>

<script>
async function startDownload() {
    const btn = document.getElementById('downloadBtn');
    const statusEl = document.getElementById('status');
    const url = document.getElementById('url').value.trim();
    if (!url) return;
    btn.disabled = true; btn.textContent = 'Downloading...';
    statusEl.className = 'status show working';
    statusEl.innerHTML = '<span class="spinner"></span> Fetching from Instagram...';
    try {
        const resp = await fetch('/api/download', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ url })
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.className = 'status show error';
            statusEl.textContent = data.error;
            btn.disabled = false; btn.textContent = 'Download';
            return;
        }
        pollStatus(data.job_id);
    } catch(e) {
        statusEl.className = 'status show error';
        statusEl.textContent = 'Request failed: ' + e.message;
        btn.disabled = false; btn.textContent = 'Download';
    }
}

function pollStatus(jobId) {
    const statusEl = document.getElementById('status');
    const btn = document.getElementById('downloadBtn');
    const interval = setInterval(async () => {
        try {
            const resp = await fetch('/api/status/' + jobId);
            const data = await resp.json();
            if (data.status === 'working') {
                statusEl.innerHTML = '<span class="spinner"></span> ' + data.message;
                return;
            }
            clearInterval(interval);
            btn.disabled = false; btn.textContent = 'Download';
            if (data.status === 'done') {
                statusEl.className = 'status show done';
                let html = data.message;
                if (data.files && data.files.length > 0) {
                    html += '<div class="file-list">';
                    data.files.forEach(f => {
                        html += '<a href="/downloads/' + encodeURIComponent(f) + '">' + f + '</a>';
                    });
                    html += '</div>';
                }
                statusEl.innerHTML = html;
            } else if (data.message && data.message.toLowerCase().includes('rate-limit')) {
                showCooldown(data.message);
            } else {
                statusEl.className = 'status show error';
                statusEl.textContent = data.message;
            }
        } catch(e) {
            clearInterval(interval);
            statusEl.className = 'status show error';
            statusEl.textContent = 'Polling failed: ' + e.message;
            btn.disabled = false; btn.textContent = 'Download';
        }
    }, 1500);
}

function showCooldown(msg) {
    const statusEl = document.getElementById('status');
    const btn = document.getElementById('downloadBtn');
    statusEl.className = 'status show error';
    const match = msg.match(/(\d+)\s*s/i);
    let secs = match ? parseInt(match[1]) : 60;
    const tick = () => {
        if (secs <= 0) {
            statusEl.innerHTML = 'Cooldown over — retrying...';
            statusEl.className = 'status show working';
            startDownload();
            return;
        }
        statusEl.innerHTML = '\u23f3 Rate-limited. Auto-retry in <strong>' + secs + 's</strong> '
            + '<button onclick="startDownload()" style="background:none;border:1px solid var(--accent);'
            + 'color:var(--accent);padding:4px 12px;border-radius:6px;cursor:pointer;font-size:13px">'
            + 'Retry Now</button>';
        secs--;
        setTimeout(tick, 1000);
    };
    tick();
}

document.getElementById('url').addEventListener('keydown', e => { if (e.key === 'Enter') startDownload(); });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print("=" * 45)
    print("  FreeInsta — Instagram Downloader")
    print(f"  Local:   http://localhost:5000")
    print(f"  Network: http://{local_ip}:5000")
    print("=" * 45)
    app.run(host="0.0.0.0", port=5000, debug=False)
