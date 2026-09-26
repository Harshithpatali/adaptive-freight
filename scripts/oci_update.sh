#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/adaptive-freight"
cd "$APP_DIR"

git pull --ff-only origin main
docker compose --env-file .env -f docker-compose.oci.yml pull postgres redis osrm caddy
docker compose --env-file .env -f docker-compose.oci.yml up -d --build
docker compose --env-file .env -f docker-compose.oci.yml ps

echo "Adaptive Freight update complete."
