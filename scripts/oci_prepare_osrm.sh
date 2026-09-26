#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="$ROOT/osrm-data"
IMAGE="ghcr.io/project-osrm/osrm-backend:v6.0.0"
PBF_URL="${OSM_PBF_URL:-https://download.geofabrik.de/north-america/us-latest.osm.pbf}"

mkdir -p "$DATA_DIR"

echo "Downloading OSM extract: $PBF_URL"
echo "A full-US extract and OSRM preprocessing require substantial disk, CPU, memory and time."

curl -L --fail --retry 5 --retry-delay 5 -o "$DATA_DIR/us-latest.osm.pbf" "$PBF_URL"

echo "Running OSRM extract..."
docker run --rm --platform linux/arm64 -v "$DATA_DIR:/data" "$IMAGE" osrm-extract -p /opt/car.lua /data/us-latest.osm.pbf

echo "Running OSRM partition..."
docker run --rm --platform linux/arm64 -v "$DATA_DIR:/data" "$IMAGE" osrm-partition /data/us-latest.osrm

echo "Running OSRM customize..."
docker run --rm --platform linux/arm64 -v "$DATA_DIR:/data" "$IMAGE" osrm-customize /data/us-latest.osrm

echo "OSRM graph prepared."
