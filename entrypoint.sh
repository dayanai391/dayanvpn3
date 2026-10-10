#!/bin/sh
set -e

PORT="${PORT:-8080}"

# Point nginx at Railway's injected PORT
sed -i "s/listen [0-9]*;/listen ${PORT};/" /etc/nginx/nginx.conf

# Make sure dirs exist
mkdir -p /etc/nginx/gen /usr/share/nginx/html/sub /data
for f in ports.map xports.map blocked.map; do
  [ -f "/etc/nginx/gen/$f" ] || touch "/etc/nginx/gen/$f"
done

# Xray respawn loop (bot can kill it to reload config)
(
  while true; do
    /usr/local/bin/xray/xray run -c /etc/xray/config.json || true
    sleep 1
  done
) &

# Bot (generates xray/nginx configs, counts traffic, enforces limits)
python3 /opt/panel/bot.py &

# nginx in the foreground
exec nginx -g "daemon off;"
