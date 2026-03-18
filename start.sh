#!/bin/bash
# ─────────────────────────────────────────────
#  Instagram Downloader — Quick Start Script
# ─────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

# Install dependencies if needed
if ! python3 -c "import instaloader" 2>/dev/null; then
    echo "[*] Installing dependencies..."
    pip install -r requirements.txt
fi
if ! python3 -c "import flask" 2>/dev/null; then
    echo "[*] Installing web dependencies..."
    pip install flask
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Instagram Video Downloader             ║"
echo "╠══════════════════════════════════════════╣"
echo "║  1) Web Interface (browser)              ║"
echo "║  2) Command Line                         ║"
echo "║  3) Quit                                 ║"
echo "╚══════════════════════════════════════════╝"
echo ""
read -p "Choose [1/2/3]: " choice

case "$choice" in
    1)
        echo ""
        echo "[*] Starting web interface at http://localhost:5000"
        echo "[*] Press Ctrl+C to stop."
        echo ""
        python3 web_app.py
        ;;
    2)
        echo ""
        read -p "Paste Instagram URL: " url
        read -p "Username (leave blank for public): " user
        if [ -n "$user" ]; then
            python3 downloader.py "$url" -u "$user" -s "session_${user}"
        else
            python3 downloader.py "$url"
        fi
        ;;
    3)
        echo "Bye!"
        exit 0
        ;;
    *)
        echo "[!] Invalid choice."
        exit 1
        ;;
esac
