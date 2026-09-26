$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force -Path osrm-data | Out-Null
$url='https://download.geofabrik.de/north-america/us-latest.osm.pbf'
Write-Host 'Downloading USA OSM extract (~11 GB; large disk/RAM requirement)...'
Invoke-WebRequest -Uri $url -OutFile osrm-data/us-latest.osm.pbf
Write-Host 'Run the official OSRM Docker image with MLD preprocessing:'
Write-Host 'docker run --rm -t -v ${PWD}/osrm-data:/data ghcr.io/project-osrm/osrm-backend:latest osrm-extract -p /opt/car.lua /data/us-latest.osm.pbf'
Write-Host 'docker run --rm -t -v ${PWD}/osrm-data:/data ghcr.io/project-osrm/osrm-backend:latest osrm-partition /data/us-latest.osrm'
Write-Host 'docker run --rm -t -v ${PWD}/osrm-data:/data ghcr.io/project-osrm/osrm-backend:latest osrm-customize /data/us-latest.osrm'
Write-Host 'Then run osrm-routed with --algorithm mld on the prepared /data/us-latest.osrm.'
