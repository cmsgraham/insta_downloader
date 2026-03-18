"""
Instagram Downloader — Multi-User Web Service
Each visitor imports their own Instagram session cookies.
Downloads are isolated per user and auto-expire.
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
from pathlib import Path

from flask import Flask, render_template_string, request, jsonify, session, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_TTL = int(os.environ.get("FILE_TTL_SECONDS", "3600"))  # 1 hour default

# Per-user state: uid -> { ig_sessionid, ig_cookies, rate_limit_until, jobs, download_dir }
_users = {}
_users_lock = threading.Lock()

# Device pairing: code -> {"uid": str, "expires": float}
_pair_codes = {}
_pair_lock = threading.Lock()


def _parse_cookie_input(raw):
    """Smart-parse user input to extract Instagram cookies.
    Accepts: raw Cookie header, cURL command, or plain sessionid value."""
    raw = raw.strip()
    cookies = {}
    # Extract Cookie header from a cURL command
    curl_match = re.search(r"-H\s+['\"]Cookie:\s*([^'\"]+)['\"]", raw, re.IGNORECASE)
    if curl_match:
        raw = curl_match.group(1)
    # Parse key=value pairs separated by semicolons
    if "=" in raw:
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    cookies[k] = v
    # If nothing parsed, treat entire input as a plain sessionid value
    if not cookies and len(raw) > 10 and ";" not in raw and "=" not in raw and " " not in raw:
        cookies["sessionid"] = raw
    return cookies


def _get_user():
    """Get or create per-user state from the Flask session."""
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
    uid = session["uid"]
    with _users_lock:
        if uid not in _users:
            user_dir = os.path.join(DOWNLOAD_DIR, uid)
            os.makedirs(user_dir, exist_ok=True)
            _users[uid] = {
                "ig_sessionid": None,
                "ig_cookies": {},
                "rate_limit_until": 0,
                "jobs": {},
                "download_dir": user_dir,
            }
        return _users[uid]


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


def _make_session(user):
    """Build a requests.Session with the user's Instagram cookies."""
    s = requests.Session()
    s.headers.update(BASE_HEADERS)
    if user["ig_sessionid"]:
        s.cookies.set("sessionid", user["ig_sessionid"], domain=".instagram.com")
    for k, v in user["ig_cookies"].items():
        if v:
            s.cookies.set(k, v, domain=".instagram.com")
    return s


def shortcode_to_media_id(shortcode):
    """Convert an Instagram shortcode to a numeric media ID."""
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
    media_id = 0
    for c in shortcode:
        media_id = media_id * 64 + alphabet.index(c)
    return media_id


def parse_url(url):
    """Parse an Instagram URL and return (content_type, identifier)."""
    url = url.strip().rstrip("/")
    # Post / Reel
    m = re.match(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if m:
        return ("post", m.group(1))
    # Single story
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/(\d+)", url)
    if m:
        return ("story", (m.group(1), m.group(2)))
    # All stories from profile
    m = re.match(r"https?://(?:www\.)?instagram\.com/stories/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))
    # Profile URL -> treat as stories
    m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?$", url)
    if m:
        return ("profile_stories", m.group(1))
    return (None, None)


def _check_rate_limit(user):
    """Raise immediately if this user is still in a cooldown window."""
    remaining = user["rate_limit_until"] - time.time()
    if remaining > 0:
        raise ConnectionAbortedError(
            f"Instagram rate-limited. Cooling down — try again in {int(remaining)+1}s."
        )


def _record_rate_limit(user, retry_after=None):
    """Record a 429 and set a per-user cooldown window."""
    cooldown = int(retry_after) if retry_after else 60
    user["rate_limit_until"] = time.time() + cooldown


def _fetch_media_v1(media_id, user):
    """Fetch media info from the v1 API. Returns parsed JSON or raises."""
    _check_rate_limit(user)
    s = _make_session(user)
    r = s.get(
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
        timeout=20,
    )
    if r.status_code == 429:
        _record_rate_limit(user, r.headers.get("Retry-After"))
        raise ConnectionAbortedError("v1 API rate-limited (429)")
    if r.status_code != 200:
        raise RuntimeError(f"v1 API returned status {r.status_code}")
    content_type = r.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise RuntimeError(
            "Instagram returned HTML instead of JSON. "
            "This usually means your session is missing or expired. "
            "Please import your Instagram session cookies first."
        )
    return r.json()


def _fetch_media_graphql(shortcode, user):
    """Fallback: fetch media via GraphQL using fb_dtsg token from page HTML."""
    _check_rate_limit(user)
    s = _make_session(user)
    page = s.get(f"https://www.instagram.com/p/{shortcode}/", timeout=20)
    if page.status_code == 429:
        _record_rate_limit(user, page.headers.get("Retry-After"))
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
        _record_rate_limit(user, r.headers.get("Retry-After"))
        raise ConnectionAbortedError("GraphQL rate-limited (429)")
    if r.status_code != 200 or not r.text:
        raise RuntimeError(f"GraphQL returned status {r.status_code}")
    content_type = r.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise RuntimeError("GraphQL returned HTML instead of JSON — session may be invalid.")
    data = r.json()
    media = data.get("data", {}).get("xdt_shortcode_media")
    if not media:
        raise RuntimeError(
            "GraphQL returned null media. "
            "Instagram now requires a valid session for all content. "
            "Please import your session cookies."
        )
    # Normalize into v1-style format so download_media_item works
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


def fetch_media_info(media_id, user, shortcode=None):
    """Fetch media info, trying v1 API first then GraphQL fallback."""
    v1_err = None
    gql_err = None

    delays = [0, 5, 15]
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            return _fetch_media_v1(media_id, user)
        except ConnectionAbortedError as e:
            v1_err = e
            continue
        except RuntimeError as e:
            v1_err = e
            break

    if shortcode:
        try:
            return _fetch_media_graphql(shortcode, user)
        except Exception as e:
            gql_err = e

    remaining = user["rate_limit_until"] - time.time()
    if remaining > 0:
        raise ConnectionAbortedError(
            f"Instagram rate-limited. Try again in ~{int(remaining)+1} seconds."
        )
    parts = ["Both v1 API and GraphQL failed."]
    if v1_err:
        parts.append(f"v1: {v1_err}")
    if gql_err:
        parts.append(f"GraphQL: {gql_err}")
    parts.append("Wait a minute and try again.")
    raise RuntimeError(" ".join(parts))


def _fetch_user_id_v1(username, user):
    """Resolve username via v1 web_profile_info API."""
    s = _make_session(user)
    r = s.get(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
        timeout=20,
    )
    if r.status_code == 429:
        raise ConnectionAbortedError("v1 user lookup rate-limited (429)")
    if r.status_code != 200:
        raise RuntimeError(f"v1 user lookup returned status {r.status_code}")
    content_type = r.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise RuntimeError("v1 user lookup returned HTML — session may be expired.")
    data = r.json()
    return data["data"]["user"]["id"]


def _fetch_user_id_search(username, user):
    """Resolve username via the search API (fallback when v1 is rate-limited)."""
    s = _make_session(user)
    r = s.get(
        "https://www.instagram.com/web/search/topsearch/",
        params={"query": username},
        timeout=20,
    )
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


def fetch_user_id(username, user):
    """Resolve a username to a numeric user ID, trying v1 API then search fallback."""
    try:
        uid = _fetch_user_id_v1(username, user)
        print(f"[+] Resolved @{username} via v1 API -> {uid}")
        return uid
    except ConnectionAbortedError as e:
        print(f"[!] v1 user lookup failed: {e} — trying search fallback")
    except RuntimeError as e:
        print(f"[!] v1 user lookup failed: {e} — trying search fallback")

    try:
        uid = _fetch_user_id_search(username, user)
        print(f"[+] Resolved @{username} via search API -> {uid}")
        return uid
    except ConnectionAbortedError:
        _record_rate_limit(user)
        raise ConnectionAbortedError(
            "Instagram rate-limited on all endpoints. Try again in ~60 seconds."
        )
    except RuntimeError as e:
        raise RuntimeError(f"Could not resolve user '{username}': {e}")


def fetch_stories(ig_user_id, user):
    """Fetch current stories for a user. Returns list of story items."""
    s = _make_session(user)
    r = s.get(
        f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={ig_user_id}",
        timeout=20,
    )
    if r.status_code == 429:
        _record_rate_limit(user, r.headers.get("Retry-After"))
        raise ConnectionAbortedError(
            "Instagram rate-limited. Try again in ~60 seconds."
        )
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


def download_file(url, filepath, user):
    """Download a URL to a local file."""
    s = _make_session(user)
    r = s.get(url, timeout=60, stream=True)
    r.raise_for_status()
    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return os.path.getsize(filepath)


def pick_best_video(video_versions):
    """Pick the highest resolution video from video_versions list."""
    if not video_versions:
        return None
    return max(video_versions, key=lambda v: v.get("width", 0) * v.get("height", 0))


def download_media_item(item, user, prefix=""):
    """Download a single media item (post, reel, or carousel). Returns list of filenames."""
    files = []
    media_type = item.get("media_type")
    code = item.get("code", prefix or "unknown")
    dl_dir = user["download_dir"]

    if media_type == 2 and "video_versions" in item:
        best = pick_best_video(item["video_versions"])
        if best:
            fname = f"{code}.mp4"
            fpath = os.path.join(dl_dir, fname)
            download_file(best["url"], fpath, user)
            files.append(fname)
    elif media_type == 8 and "carousel_media" in item:
        for i, sub in enumerate(item["carousel_media"]):
            sub_code = f"{code}_slide{i+1}"
            if sub.get("media_type") == 2 and "video_versions" in sub:
                best = pick_best_video(sub["video_versions"])
                if best:
                    fname = f"{sub_code}.mp4"
                    fpath = os.path.join(dl_dir, fname)
                    download_file(best["url"], fpath, user)
                    files.append(fname)
            elif "image_versions2" in sub:
                candidates = sub["image_versions2"].get("candidates", [])
                if candidates:
                    best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
                    fname = f"{sub_code}.jpg"
                    fpath = os.path.join(dl_dir, fname)
                    download_file(best_img["url"], fpath, user)
                    files.append(fname)
    elif media_type == 1 and "image_versions2" in item:
        candidates = item["image_versions2"].get("candidates", [])
        if candidates:
            best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            fname = f"{code}.jpg"
            fpath = os.path.join(dl_dir, fname)
            download_file(best_img["url"], fpath, user)
            files.append(fname)

    return files


def download_story_item(item, user, username="story"):
    """Download a single story item. Returns list of filenames."""
    files = []
    story_id = item.get("pk", item.get("id", "unknown"))
    dl_dir = user["download_dir"]

    if item.get("media_type") == 2 and "video_versions" in item:
        best = pick_best_video(item["video_versions"])
        if best:
            fname = f"{username}_story_{story_id}.mp4"
            fpath = os.path.join(dl_dir, fname)
            download_file(best["url"], fpath, user)
            files.append(fname)
    elif "image_versions2" in item:
        candidates = item["image_versions2"].get("candidates", [])
        if candidates:
            best_img = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            fname = f"{username}_story_{story_id}.jpg"
            fpath = os.path.join(dl_dir, fname)
            download_file(best_img["url"], fpath, user)
            files.append(fname)

    return files


# ──────────────────────────────────────────────
#  Background download worker
# ──────────────────────────────────────────────

def run_download(job_id, url, user):
    """Background worker that performs the download."""
    try:
        content_type, identifier = parse_url(url)
        if content_type is None:
            user["jobs"][job_id] = {"status": "error", "message": f"Could not parse URL: {url}", "files": []}
            return

        if not user["ig_sessionid"]:
            user["jobs"][job_id] = {
                "status": "error",
                "message": "Instagram requires login for all content. "
                           "Please import your session cookies first using the 'Import Session' button above.",
                "files": [],
            }
            return

        all_files = []

        if content_type == "post":
            shortcode = identifier
            media_id = shortcode_to_media_id(shortcode)
            data = fetch_media_info(media_id, user, shortcode=shortcode)

            if not data.get("items"):
                user["jobs"][job_id] = {"status": "error", "message": "Media not found or unavailable.", "files": []}
                return

            item = data["items"][0]
            all_files = download_media_item(item, user, prefix=shortcode)

        elif content_type == "story":
            username, story_pk = identifier
            ig_user_id = fetch_user_id(username, user)
            items = fetch_stories(ig_user_id, user)

            found = False
            for item in items:
                if str(item.get("pk")) == story_pk or str(item.get("id")) == story_pk:
                    all_files = download_story_item(item, user, username)
                    found = True
                    break

            if not found:
                user["jobs"][job_id] = {"status": "error", "message": f"Story {story_pk} not found. It may have expired.", "files": []}
                return

        elif content_type == "profile_stories":
            username = identifier
            ig_user_id = fetch_user_id(username, user)
            items = fetch_stories(ig_user_id, user)

            if not items:
                user["jobs"][job_id] = {"status": "error", "message": f"No active stories found for @{username}.", "files": []}
                return

            for item in items:
                all_files.extend(download_story_item(item, user, username))

        if all_files:
            user["jobs"][job_id] = {"status": "done", "message": f"Downloaded {len(all_files)} file(s).", "files": all_files}
        else:
            user["jobs"][job_id] = {"status": "done", "message": "No downloadable media found.", "files": []}

    except ConnectionAbortedError as e:
        user["jobs"][job_id] = {"status": "error", "message": str(e), "files": []}
    except Exception as e:
        user["jobs"][job_id] = {"status": "error", "message": str(e), "files": []}


# ──────────────────────────────────────────────
#  Periodic cleanup of expired downloads
# ──────────────────────────────────────────────

def _cleanup_loop():
    """Remove downloaded files older than FILE_TTL, prune empty user dirs, and expire pair codes."""
    while True:
        time.sleep(300)  # check every 5 minutes
        try:
            now = time.time()
            for uid_dir in Path(DOWNLOAD_DIR).iterdir():
                if not uid_dir.is_dir():
                    continue
                for f in uid_dir.iterdir():
                    if f.is_file() and (now - f.stat().st_mtime) > FILE_TTL:
                        f.unlink(missing_ok=True)
                # Remove dir if empty
                if uid_dir.is_dir() and not any(uid_dir.iterdir()):
                    uid_dir.rmdir()
            # Prune expired pair codes
            with _pair_lock:
                expired = [c for c, v in _pair_codes.items() if now > v["expires"]]
                for c in expired:
                    del _pair_codes[c]
        except Exception:
            pass

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    _get_user()  # ensure session cookie is set
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/download", methods=["POST"])
def api_download():
    user = _get_user()
    data = request.get_json(force=True)
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = uuid.uuid4().hex[:12]
    user["jobs"][job_id] = {"status": "working", "message": "Downloading...", "files": []}

    thread = threading.Thread(target=run_download, args=(job_id, url, user), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    user = _get_user()
    job = user["jobs"].get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/import-session", methods=["POST"])
def api_import_session():
    """Import a session — accepts raw cookie string, cURL, or plain sessionid."""
    user = _get_user()
    data = request.get_json(force=True)
    raw = data.get("raw", "").strip()

    if not raw:
        return jsonify({"error": "Please paste your cookie string."}), 400

    cookies = _parse_cookie_input(raw)
    sessionid = cookies.get("sessionid", "")

    if not sessionid:
        return jsonify({"error": "No sessionid found. Make sure you copied the full Cookie header."}), 400
    if len(sessionid) < 10:
        return jsonify({"error": "sessionid looks too short — double-check the value."}), 400

    user["ig_sessionid"] = sessionid
    user["ig_cookies"] = {k: v for k, v in cookies.items() if k != "sessionid"}

    return jsonify({"ok": True, "message": "Connected! You can now download content."})


@app.route("/api/session-status")
def api_session_status():
    user = _get_user()
    remaining = max(0, int(user["rate_limit_until"] - time.time()))
    return jsonify({
        "logged_in": user["ig_sessionid"] is not None,
        "user": user["ig_cookies"].get("ds_user_id", "imported") if user["ig_sessionid"] else None,
        "cooldown": remaining,
    })


@app.route("/api/generate-pair-code", methods=["POST"])
def api_generate_pair_code():
    """Generate a 6-digit code to pair a mobile device."""
    user = _get_user()
    uid = session["uid"]
    if not user["ig_sessionid"]:
        return jsonify({"error": "Import your session first."}), 400
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    with _pair_lock:
        # Invalidate any previous codes for this user
        to_del = [c for c, v in _pair_codes.items() if v["uid"] == uid]
        for c in to_del:
            del _pair_codes[c]
        _pair_codes[code] = {"uid": uid, "expires": time.time() + 300}
    return jsonify({"code": code, "expires_in": 300})


@app.route("/api/pair", methods=["POST"])
def api_pair():
    """Pair this device with another session using a 6-digit code."""
    user = _get_user()
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    if not code or len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Enter a valid 6-digit code."}), 400
    with _pair_lock:
        entry = _pair_codes.get(code)
        if not entry or time.time() > entry["expires"]:
            return jsonify({"error": "Invalid or expired code."}), 400
        source_uid = entry["uid"]
        del _pair_codes[code]
    with _users_lock:
        source = _users.get(source_uid)
        if not source or not source["ig_sessionid"]:
            return jsonify({"error": "Source session is no longer valid."}), 400
        user["ig_sessionid"] = source["ig_sessionid"]
        user["ig_cookies"] = dict(source["ig_cookies"])
    return jsonify({"ok": True, "message": "Device linked! You're now connected."})


@app.route("/downloads/<path:filename>")
def serve_file(filename):
    user = _get_user()
    return send_from_directory(user["download_dir"], filename, as_attachment=True)


# ──────────────────────────────────────────────
#  HTML Template
# ──────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1">
<title>Instagram Downloader</title>
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
    .container { width: 100%; max-width: 560px; }
    .logo { text-align: center; margin-bottom: 30px; }
    .logo h1 {
        font-size: 26px;
        background: linear-gradient(45deg, var(--accent), var(--accent2));
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .logo p { color: var(--muted); font-size: 14px; margin-top: 6px; }
    .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 24px;
        margin-bottom: 16px;
    }
    .session-bar {
        display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
        padding: 12px 14px; border-radius: 10px; margin-bottom: 18px; font-size: 13px;
    }
    .session-bar.logged-in { background: #0a2e1a; border: 1px solid #1a5e3a; }
    .session-bar.logged-out { background: #2e1a0a; border: 1px solid #5e3a1a; }
    .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 8px; }
    .dot.green { background: #4caf50; }
    .dot.orange { background: #ff9800; }
    .session-actions { display: flex; gap: 10px; }
    .toggle-btn {
        background: none; border: none; color: var(--accent); cursor: pointer;
        font-size: 13px; padding: 4px 0; -webkit-tap-highlight-color: transparent;
    }
    .toggle-btn:hover { text-decoration: underline; }
    .tabs { display: flex; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
    .tab {
        flex: 1; padding: 12px; text-align: center; font-size: 14px; font-weight: 500;
        background: none; border: none; color: var(--muted); cursor: pointer;
        border-bottom: 2px solid transparent; transition: all 0.2s;
        -webkit-tap-highlight-color: transparent;
    }
    .tab.active { color: var(--text); border-bottom-color: var(--accent); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .field { margin-bottom: 18px; }
    .field label {
        display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px;
        text-transform: uppercase; letter-spacing: 0.5px;
    }
    .field input, .field textarea {
        width: 100%; padding: 14px; background: var(--bg); border: 1px solid var(--border);
        border-radius: 10px; color: var(--text); font-size: 16px; outline: none;
        transition: border-color 0.2s; font-family: inherit; resize: vertical;
    }
    .field input:focus, .field textarea:focus { border-color: var(--accent); }
    .field input::placeholder, .field textarea::placeholder { color: #555; }
    .hint { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.5; }
    .btn {
        width: 100%; padding: 16px;
        background: linear-gradient(45deg, var(--accent), var(--accent2));
        color: #fff; border: none; border-radius: 10px; font-size: 16px; font-weight: 600;
        cursor: pointer; transition: opacity 0.2s; -webkit-tap-highlight-color: transparent;
    }
    .btn:hover { opacity: 0.9; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-outline {
        width: 100%; padding: 14px; background: transparent;
        border: 1px solid var(--border); border-radius: 10px;
        color: var(--text); font-size: 15px; font-weight: 500;
        cursor: pointer; transition: border-color 0.2s; -webkit-tap-highlight-color: transparent;
    }
    .btn-outline:hover { border-color: var(--accent); }
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
    .instructions {
        background: #111; border: 1px solid var(--border); border-radius: 10px;
        padding: 16px; margin-bottom: 18px; font-size: 13px; line-height: 1.7;
    }
    .instructions ol { padding-left: 20px; }
    .instructions code {
        background: #222; padding: 2px 6px; border-radius: 4px;
        font-size: 12px; color: var(--accent);
    }
    .pair-code-display { text-align: center; padding: 20px 0; }
    .pair-code-display .code {
        font-size: 42px; font-weight: 700; letter-spacing: 12px;
        font-family: 'SF Mono', 'Consolas', monospace;
        background: linear-gradient(45deg, var(--accent), var(--accent2));
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        margin: 16px 0;
    }
    .pair-code-display .expires { color: var(--muted); font-size: 13px; }
    .code-input {
        text-align: center; font-size: 28px; letter-spacing: 10px; font-weight: 600;
        font-family: 'SF Mono', 'Consolas', monospace;
    }
    .collapsible { display: none; }
    .collapsible.open { display: block; }
    .footer-hint {
        text-align: center; margin-top: 16px; font-size: 12px;
        color: var(--muted); line-height: 1.6;
    }
</style>
</head>
<body>
<div class="container">
    <div class="logo">
        <h1>Instagram Downloader</h1>
        <p>Download reels, posts &amp; stories &mdash; free &amp; private</p>
    </div>

    <!-- Session status -->
    <div id="sessionBar" class="session-bar logged-out">
        <span><span class="dot orange" id="statusDot"></span><span id="sessionText">Not connected</span></span>
        <div class="session-actions">
            <button class="toggle-btn" onclick="toggleImport()" id="importToggleBtn">Connect</button>
            <button class="toggle-btn" onclick="togglePairDisplay()" id="pairDisplayBtn" style="display:none">&#128241; Link Mobile</button>
        </div>
    </div>

    <!-- Pair code display (desktop &rarr; mobile) -->
    <div class="card collapsible" id="pairDisplayCard">
        <div class="pair-code-display">
            <div style="font-size:14px;color:var(--muted);margin-bottom:4px">Enter this code on your phone</div>
            <div class="code" id="pairCodeValue">------</div>
            <div class="expires" id="pairExpiry">Generating...</div>
        </div>
        <button class="btn-outline" onclick="generatePairCode()" id="refreshCodeBtn">Generate New Code</button>
    </div>

    <!-- Import / Pair card -->
    <div class="card collapsible" id="importCard">
        <div class="tabs">
            <button class="tab active" onclick="switchTab('paste')" id="tabPaste">Paste Cookies</button>
            <button class="tab" onclick="switchTab('code')" id="tabCode">Enter Code</button>
        </div>

        <!-- Tab 1: Paste cookies -->
        <div class="tab-content active" id="panePaste">
            <div class="instructions">
                <strong>How to get your cookies:</strong>
                <ol>
                    <li>Open <a href="https://www.instagram.com" target="_blank" style="color:var(--accent)">instagram.com</a> and log in</li>
                    <li>Press <code>F12</code> &rarr; <strong>Network</strong> tab</li>
                    <li>Refresh the page, click any request</li>
                    <li>Find the <code>Cookie</code> header &rarr; copy its full value</li>
                    <li>Paste it below</li>
                </ol>
            </div>
            <div class="field">
                <label>Cookie string</label>
                <textarea id="cookieInput" rows="3" placeholder="sessionid=abc123; csrftoken=xyz; ds_user_id=..."></textarea>
                <div class="hint">Paste the full Cookie header, a cURL command, or just the sessionid value.</div>
            </div>
            <button class="btn" onclick="importSession()" id="importBtn">Connect to Instagram</button>
            <div class="status" id="importStatus"></div>
        </div>

        <!-- Tab 2: Enter pair code -->
        <div class="tab-content" id="paneCode">
            <div class="instructions">
                <strong>Link from another device:</strong>
                <ol>
                    <li>On a computer, open this site and import your cookies</li>
                    <li>Click <strong>&#128241; Link Mobile</strong> to get a 6-digit code</li>
                    <li>Enter the code below</li>
                </ol>
            </div>
            <div class="field">
                <label>6-digit code</label>
                <input type="text" id="pairCodeInput" class="code-input"
                       inputmode="numeric" pattern="[0-9]*" maxlength="6"
                       placeholder="000000" autocomplete="off">
            </div>
            <button class="btn" onclick="pairDevice()" id="pairBtn">Link Device</button>
            <div class="status" id="pairStatus"></div>
        </div>
    </div>

    <!-- Download -->
    <div class="card">
        <div class="field">
            <label>Instagram URL</label>
            <input type="url" id="url" placeholder="https://www.instagram.com/reel/..." autofocus>
        </div>
        <button class="btn" id="downloadBtn" onclick="startDownload()">Download</button>
        <div class="status" id="status"></div>
    </div>

    <div class="footer-hint">
        Your cookies are used only to fetch content and are never stored to disk.<br>
        Downloads auto-expire after 1 hour.
    </div>
</div>

<script>
/* ── Tabs ── */
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(p => p.classList.remove('active'));
    if (tab === 'paste') {
        document.getElementById('tabPaste').classList.add('active');
        document.getElementById('panePaste').classList.add('active');
    } else {
        document.getElementById('tabCode').classList.add('active');
        document.getElementById('paneCode').classList.add('active');
    }
}

/* ── Session check ── */
async function checkSession() {
    try {
        const resp = await fetch('/api/session-status');
        const data = await resp.json();
        const bar = document.getElementById('sessionBar');
        const dot = document.getElementById('statusDot');
        const text = document.getElementById('sessionText');
        const importBtn = document.getElementById('importToggleBtn');
        const pairBtn = document.getElementById('pairDisplayBtn');
        if (data.logged_in) {
            bar.className = 'session-bar logged-in';
            dot.className = 'dot green';
            text.textContent = 'Connected to Instagram';
            importBtn.textContent = 'Change';
            pairBtn.style.display = '';
            document.getElementById('importCard').classList.remove('open');
        } else {
            bar.className = 'session-bar logged-out';
            dot.className = 'dot orange';
            text.textContent = 'Not connected';
            importBtn.textContent = 'Connect';
            pairBtn.style.display = 'none';
            document.getElementById('importCard').classList.add('open');
        }
    } catch(e) {}
}
checkSession();

function toggleImport() {
    document.getElementById('importCard').classList.toggle('open');
    document.getElementById('pairDisplayCard').classList.remove('open');
}

function togglePairDisplay() {
    const card = document.getElementById('pairDisplayCard');
    card.classList.toggle('open');
    document.getElementById('importCard').classList.remove('open');
    if (card.classList.contains('open')) generatePairCode();
}

/* ── Import session (smart paste) ── */
async function importSession() {
    const raw = document.getElementById('cookieInput').value.trim();
    const statusEl = document.getElementById('importStatus');
    const btn = document.getElementById('importBtn');
    if (!raw) { alert('Please paste your cookie string.'); return; }
    btn.disabled = true; btn.textContent = 'Connecting...';
    statusEl.className = 'status show working';
    statusEl.innerHTML = '<span class="spinner"></span> Importing session...';
    try {
        const resp = await fetch('/api/import-session', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ raw })
        });
        const data = await resp.json();
        if (data.ok) {
            statusEl.className = 'status show done';
            statusEl.textContent = data.message;
            checkSession();
        } else {
            statusEl.className = 'status show error';
            statusEl.textContent = data.error || 'Import failed';
        }
    } catch(e) {
        statusEl.className = 'status show error';
        statusEl.textContent = 'Request failed: ' + e.message;
    }
    btn.disabled = false; btn.textContent = 'Connect to Instagram';
}

/* ── Generate pair code ── */
let pairCountdown = null;
async function generatePairCode() {
    const codeEl = document.getElementById('pairCodeValue');
    const expiryEl = document.getElementById('pairExpiry');
    try {
        const resp = await fetch('/api/generate-pair-code', { method: 'POST' });
        const data = await resp.json();
        if (data.error) { expiryEl.textContent = data.error; return; }
        codeEl.textContent = data.code;
        if (pairCountdown) clearInterval(pairCountdown);
        let secs = data.expires_in;
        const fmt = s => Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
        expiryEl.textContent = 'Expires in ' + fmt(secs);
        pairCountdown = setInterval(() => {
            secs--;
            if (secs <= 0) {
                clearInterval(pairCountdown);
                codeEl.textContent = '------';
                expiryEl.textContent = 'Code expired';
                return;
            }
            expiryEl.textContent = 'Expires in ' + fmt(secs);
        }, 1000);
    } catch(e) { expiryEl.textContent = 'Failed to generate code'; }
}

/* ── Pair device ── */
async function pairDevice() {
    const code = document.getElementById('pairCodeInput').value.trim();
    const statusEl = document.getElementById('pairStatus');
    const btn = document.getElementById('pairBtn');
    if (!code || code.length !== 6) { alert('Enter the 6-digit code from your other device.'); return; }
    btn.disabled = true; btn.textContent = 'Linking...';
    statusEl.className = 'status show working';
    statusEl.innerHTML = '<span class="spinner"></span> Linking device...';
    try {
        const resp = await fetch('/api/pair', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ code })
        });
        const data = await resp.json();
        if (data.ok) {
            statusEl.className = 'status show done';
            statusEl.textContent = data.message;
            checkSession();
        } else {
            statusEl.className = 'status show error';
            statusEl.textContent = data.error || 'Pairing failed';
        }
    } catch(e) {
        statusEl.className = 'status show error';
        statusEl.textContent = 'Request failed: ' + e.message;
    }
    btn.disabled = false; btn.textContent = 'Link Device';
}

/* ── Download ── */
async function startDownload() {
    const btn = document.getElementById('downloadBtn');
    const statusEl = document.getElementById('status');
    const url = document.getElementById('url').value.trim();
    if (!url) { alert('Please paste an Instagram URL.'); return; }
    btn.disabled = true; btn.textContent = 'Downloading...';
    statusEl.className = 'status show working';
    statusEl.innerHTML = '<span class="spinner"></span> Starting download...';
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
            statusEl.innerHTML = 'Cooldown over \u2014 retrying...';
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

/* ── Keyboard shortcuts ── */
document.getElementById('url').addEventListener('keydown', e => { if (e.key === 'Enter') startDownload(); });
document.getElementById('pairCodeInput').addEventListener('keydown', e => { if (e.key === 'Enter') pairDevice(); });
document.getElementById('cookieInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); importSession(); }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print("=" * 45)
    print("  Instagram Downloader - Web Interface")
    print(f"  Local:   http://localhost:5000")
    print(f"  Network: http://{local_ip}:5000")
    print("=" * 45)
    app.run(host="0.0.0.0", port=5000, debug=False)
