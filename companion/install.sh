#!/usr/bin/env bash
# Install in Hisho (src/hisho/companion.py runs this on the Core machine: this Souko when standalone, else Kihon):
# Vikunja in Docker with its own SQLite database, on 127.0.0.1:3456, published on the tailnet only at
# https://<this machine's tailnet name>:8456. Data in ~/.local/share/hisho/vikunja (kept on remove).
# After it: open that address, register the first user (it becomes the admin), then add the entry with that
# user on the Vikunja page (or an API token from Vikunja > Settings > API Tokens).
set -euo pipefail
data=~/.local/share/hisho/vikunja
image=vikunja/vikunja:0.24.6
mkdir -p "$data/files" "$data/db"
dns=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
[ -n "$dns" ] || { echo "connect this machine to Tailscale first (Set up your Hisho > Tailscale)" >&2; exit 1; }
umask 077
[ -f "$data/.secret" ] || openssl rand -hex 32 > "$data/.secret"
cat > "$data/docker-compose.yml" <<YAML
services:
  vikunja:
    image: $image
    container_name: vikunja
    restart: unless-stopped
    environment:
      VIKUNJA_SERVICE_PUBLICURL: https://$dns:8456/
      VIKUNJA_SERVICE_JWTSECRET_FILE: /app/vikunja/.secret
      VIKUNJA_DATABASE_TYPE: sqlite
      VIKUNJA_DATABASE_PATH: /db/vikunja.db
    ports: ["127.0.0.1:3456:3456"]
    volumes:
      - ./files:/app/vikunja/files
      - ./db:/db
      - ./.secret:/app/vikunja/.secret:ro
YAML
docker compose -f "$data/docker-compose.yml" up -d
for i in $(seq 1 60); do curl -fs -m 3 -o /dev/null http://127.0.0.1:3456/api/v1/info && break; sleep 3; done
tailscale serve --bg --https=8456 http://127.0.0.1:3456 >/dev/null 2>&1 \
  || sudo -n tailscale serve --bg --https=8456 http://127.0.0.1:3456 >/dev/null
echo "Vikunja: https://$dns:8456/ (tailnet only). Register the first user there."
