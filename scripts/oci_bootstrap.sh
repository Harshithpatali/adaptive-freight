#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/adaptive-freight"

sudo apt-get update
sudo apt-get install -y ca-certificates curl git docker.io docker-compose-plugin
sudo systemctl enable --now docker

sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone https://github.com/Harshithpatali/adaptive-freight.git "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only origin main
fi

cd "$APP_DIR"
cp -n .env.oci.example .env || true
mkdir -p osrm-data

echo "Base OCI setup complete."
echo "Edit $APP_DIR/.env, then prepare OSRM and start docker compose."
