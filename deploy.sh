#!/bin/bash
# Deploy script - run from WSL:  bash deploy.sh
set -e

SERVER="cmsgraham@172.233.171.101"
KEY="$HOME/.ssh/key"

echo "=== Step 1: Removing passphrase from SSH key ==="
echo "Enter current passphrase, then press Enter twice for no new passphrase:"
ssh-keygen -p -f "$KEY"

echo ""
echo "=== Step 2: Copying YouTube cookies to server ==="
scp -i "$KEY" www.youtube.com_cookies.txt "$SERVER:~/insta_downloader/www.youtube.com_cookies.txt"

echo ""
echo "=== Step 3: Deploying (pull + rebuild + restart) ==="
ssh -i "$KEY" "$SERVER" "cd insta_downloader && git pull && docker compose down && docker compose up -d --build"

echo ""
echo "=== Done! Verifying ==="
ssh -i "$KEY" "$SERVER" "cd insta_downloader && git log --oneline -1 && docker ps --format '{{.Names}} {{.Status}}' | grep insta"
