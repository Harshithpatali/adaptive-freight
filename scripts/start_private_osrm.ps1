$ErrorActionPreference = "Stop"
if (-not (Test-Path .\osrm-data\us-latest.osrm)) { throw "Prepared OSRM graph not found. Run .\scripts\prepare_osrm.ps1 first and complete extract/partition/customize." }
docker run --rm -it -p 5000:5000 -v "${PWD}\osrm-data:/data" ghcr.io/project-osrm/osrm-backend:latest osrm-routed --algorithm mld /data/us-latest.osrm
