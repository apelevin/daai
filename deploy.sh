#!/usr/bin/env bash
# Deploy: pull latest code and rebuild/restart agent container.
set -e
cd "$(dirname "$0")"

echo "→ git pull"
git pull

echo "→ docker compose up -d --build"
docker compose up -d --build

echo "→ done. Logs: docker compose logs -f agent"
