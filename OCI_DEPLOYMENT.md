# Oracle Cloud Always Free Deployment

This deployment runs the Adaptive Freight backend as a persistent Docker stack on an OCI Ampere A1 VM:

- FastAPI realtime backend
- PostgreSQL persistence
- Redis event bus
- private OSRM routing
- Caddy HTTPS
- Docker restart policies

The Streamlit UI can remain on Streamlit Community Cloud and connect to the OCI HTTPS API.

## OCI VM

Use Ubuntu on an Always Free VM.Standard.A1.Flex instance.

Recommended initial layout:

    2 OCPU
    12 GB RAM
    100-200 GB block volume

A full-US OSRM graph is the storage-heavy component.

## Network

Allow:

    TCP 80
    TCP 443
    TCP 22 from your own IP only

Do not expose:

    5432 PostgreSQL
    6379 Redis
    5000 OSRM
    8000 FastAPI

These services remain on the Docker private network.

## DNS

Create an A record:

    api.yourdomain.com -> OCI VM public IP

Caddy will automatically obtain and renew a public HTTPS certificate when DNS and ports 80/443 are correctly configured.

## Install

SSH to the VM:

    git clone https://github.com/Harshithpatali/adaptive-freight.git /opt/adaptive-freight
    cd /opt/adaptive-freight
    bash scripts/oci_bootstrap.sh

Edit:

    nano /opt/adaptive-freight/.env

Set:

    DOMAIN=api.yourdomain.com
    ACME_EMAIL=you@example.com
    POSTGRES_PASSWORD=<long-random-password>
    API_KEYS=sk_admin:admin,sk_dispatch:dispatcher,sk_view:readonly
    CORS_ORIGINS=https://your-streamlit-app.streamlit.app

## Private OSRM

Prepare the routing graph:

    bash scripts/oci_prepare_osrm.sh

Then start:

    docker compose --env-file .env -f docker-compose.oci.yml up -d --build

Check:

    docker compose --env-file .env -f docker-compose.oci.yml ps
    docker compose --env-file .env -f docker-compose.oci.yml logs --tail=100 api
    curl https://api.yourdomain.com/health
    curl https://api.yourdomain.com/version

The API uses:

    ROUTER_URL=http://osrm:5000

so route requests stay on the OCI private Docker network.

## Streamlit

Set Streamlit secrets:

    BACKEND_URL="https://api.yourdomain.com"
    PUBLIC_BACKEND_URL="https://api.yourdomain.com"
    DASHBOARD_API_KEY="sk_view"

The same public HTTPS API is used for the embedded map and browser WebSocket.

## Updating

    cd /opt/adaptive-freight
    git pull --ff-only origin main
    docker compose --env-file .env -f docker-compose.oci.yml up -d --build

Docker services use restart policies so the stack returns after a VM or Docker restart.

## PostgreSQL backup

    docker compose --env-file .env -f docker-compose.oci.yml exec -T postgres pg_dump -U freight -d freight > backup.sql

## OSRM note

The private routing workflow uses a Geofabrik US extract. The PBF and generated OSRM graph are deliberately not committed to Git.

For a smaller initial test, set OSM_PBF_URL to a regional extract covering the cities you need, then use that prepared graph with the same compose stack.
