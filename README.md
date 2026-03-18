# Instagram Video Downloader

Download videos from Instagram — **stories**, **reels**, and **posts** — via the command line.  
Supports login for accessing **private accounts** and **stories**.

## Setup

```bash
# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate    # Linux/Mac
# venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

## Usage

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

**Save session to avoid re-entering password:**
```bash
# First time — prompts for password, saves session
python3 downloader.py https://www.instagram.com/reel/ABC123/ -u your_username -s my_session

# Next time — reuses saved session, no password prompt
python3 downloader.py https://www.instagram.com/reel/XYZ789/ -u your_username -s my_session
```

## Supported URL Formats

| Type | URL Pattern |
|------|------------|
| Post | `https://www.instagram.com/p/SHORTCODE/` |
| Reel | `https://www.instagram.com/reel/SHORTCODE/` |
| Story | `https://www.instagram.com/stories/USERNAME/STORY_ID/` |
| All stories | `https://www.instagram.com/USERNAME/` |

## Notes

- **Stories require login** — Instagram doesn't expose stories without authentication.
- **Private accounts require login** — you must follow the account and log in with your credentials.
- **2FA is supported** — if your account uses two-factor auth, you'll be prompted for the code.
- **Session files** store your login cookies locally so you don't need to enter your password every time. Keep them secure.
- Downloaded files go into the `downloads/` folder by default (configurable with `-o`).
