# FreeDL — Video Downloader

Download videos from **Instagram**, **Twitter/X**, and **YouTube** — via the web app or command line.

## Web App

The main interface is a self-hosted web app. Users paste a URL and download instantly.

**Supported platforms:**
- **Instagram** — reels, posts (images + video), stories (requires server cookies)
- **Twitter/X** — tweet videos (`twitter.com`, `x.com`, `t.co` short links)
- **YouTube** — videos, shorts (`youtube.com`, `youtu.be`)

### Running with Docker

```bash
docker compose up -d
```

The web app will be available at `http://localhost:5000`.

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key | random |
| `FILE_TTL_SECONDS` | Auto-delete downloads after N seconds | `3600` |
| `ADMIN_TOKEN` | Bearer token for `/api/admin/reload-cookies` | — |
| `COOKIES_FILE` | Path to Instagram cookies.txt | `/app/cookies.txt` |
| `RATE_LIMIT_PER_MIN` | Max downloads per visitor per minute | `10` |

### Instagram cookies

Instagram downloads require a `cookies.txt` file with a valid session. Export it from your browser (Netscape format) and place it in the project root.

---

## CLI Downloader (Instagram only)

The `downloader.py` script provides command-line Instagram downloads with login support.

```
python3 downloader.py <URL> [options]
```

### Options

| Flag | Description |
|------|-----------|
| `--username`, `-u` | Instagram username (required for private content & stories) |
| `--session`, `-s` | Path to session file (saves login so you don't re-enter password each time) |
| `--output`, `-o` | Output directory (default: `downloads/`) |

### Examples

**Download a public reel:**
```bash
python3 downloader.py https://www.instagram.com/reel/ABC123/
```

**Download from a private account:**
```bash
python3 downloader.py https://www.instagram.com/reel/ABC123/ -u your_username
```

**Download a specific story:**
```bash
python3 downloader.py https://www.instagram.com/stories/someuser/1234567890/ -u your_username
```

**Download all current stories from a user:**
```bash
python3 downloader.py https://www.instagram.com/someuser/ -u your_username
```

---

## Supported URL Formats

| Platform | Type | URL Pattern |
|----------|------|------------|
| Instagram | Post | `https://www.instagram.com/p/SHORTCODE/` |
| Instagram | Reel | `https://www.instagram.com/reel/SHORTCODE/` |
| Instagram | Story | `https://www.instagram.com/stories/USERNAME/STORY_ID/` |
| Instagram | All stories | `https://www.instagram.com/USERNAME/` |
| Twitter/X | Tweet | `https://twitter.com/USER/status/ID` |
| Twitter/X | Tweet | `https://x.com/USER/status/ID` |
| YouTube | Video | `https://www.youtube.com/watch?v=VIDEO_ID` |
| YouTube | Short | `https://youtube.com/shorts/VIDEO_ID` |
| YouTube | Short link | `https://youtu.be/VIDEO_ID` |

## Notes

- **Instagram stories require cookies** — Instagram doesn't expose stories without authentication.
- **Private Instagram accounts require cookies** — your session must follow the account.
- **Twitter/X and YouTube** downloads use `yt-dlp` and require **no authentication**.
- **2FA is supported** for the CLI downloader.
- Downloaded files auto-expire after 1 hour (configurable).
