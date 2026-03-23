#!/bin/sh
# Watchdog — monitors the app container and restarts it if unresponsive.
# Runs inside a minimal docker:cli container with access to the Docker socket.

TARGET="${TARGET_CONTAINER:-insta_downloader-app-1}"
URL="${HEALTH_URL:-http://app:5000/api/health}"
INTERVAL="${CHECK_INTERVAL:-60}"
MAX_FAIL="${MAX_FAILURES:-3}"

fail_count=0

echo "[watchdog] Monitoring ${TARGET} via ${URL} every ${INTERVAL}s (max ${MAX_FAIL} failures)"

while true; do
    sleep "$INTERVAL"

    # Use wget since docker:cli image has it (no curl)
    if wget -q -T 5 -O /dev/null "$URL" 2>/dev/null; then
        if [ "$fail_count" -gt 0 ]; then
            echo "[watchdog] $(date -u '+%Y-%m-%d %H:%M:%S') App recovered after ${fail_count} failure(s)"
        fi
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        echo "[watchdog] $(date -u '+%Y-%m-%d %H:%M:%S') Health check failed (${fail_count}/${MAX_FAIL})"

        if [ "$fail_count" -ge "$MAX_FAIL" ]; then
            echo "[watchdog] $(date -u '+%Y-%m-%d %H:%M:%S') Restarting ${TARGET}..."
            docker restart "$TARGET"
            fail_count=0
            # Wait for container to come back up
            sleep 15
        fi
    fi
done
