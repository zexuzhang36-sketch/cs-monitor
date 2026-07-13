#!/bin/bash
set -e

PORT="${PORT:-5000}"
CF="/tmp/cloudflared"

# Download cloudflared if not present
if [ ! -f "$CF" ]; then
    echo "[start] Downloading cloudflared..."
    curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o "$CF"
    chmod +x "$CF"
    echo "[start] cloudflared ready"
fi

# Start cloudflared tunnel in background
echo "[start] Starting Cloudflare Tunnel -> localhost:$PORT"
$CF tunnel --url "http://localhost:$PORT" --no-autoupdate 2>&1 | while IFS= read -r line; do
    echo "[tunnel] $line"
    if echo "$line" | grep -q "trycloudflare\.com"; then
        echo "$line" | sed -n 's|.*\(https://[^ ]*\.trycloudflare\.com\).*|\1|p' > /tmp/tunnel_url.txt
        echo "[tunnel] URL saved: $(cat /tmp/tunnel_url.txt)"
    fi
done &

# Wait for tunnel URL (max 60s)
for i in $(seq 1 30); do
    if [ -s /tmp/tunnel_url.txt ]; then
        echo "[start] Tunnel ready: $(cat /tmp/tunnel_url.txt)"
        break
    fi
    echo "[start] Waiting for tunnel... ($((i*2))s)"
    sleep 2
done

echo "[start] Starting gunicorn on port $PORT"
exec gunicorn app:app --bind "0.0.0.0:$PORT" --preload --workers 1 --timeout 120
