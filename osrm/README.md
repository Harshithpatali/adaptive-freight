# Private OSRM

The production routing path is a private OSRM instance rather than the public demo server.

OSRM MLD preprocessing:
osrm-extract -> osrm-partition -> osrm-customize

For development, point ROUTER_URL at a regional/private OSRM instance or temporarily use the public endpoint.

For a full-USA graph, obtain the Geofabrik US PBF with scripts/prepare_osrm.ps1 and complete the preprocessing commands printed by that script.
