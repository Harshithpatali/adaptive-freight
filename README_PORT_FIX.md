# Port / launcher fix

run.py handles Windows port conflicts safely:

- Reuses a healthy Adaptive Freight backend already running on the requested port.
- Selects the next free local port if another process owns the requested port.
- Prints the selected dashboard URL.

Typical run:

    python run.py

To force a specific port:

    $env:APP_PORT=8010
    python run.py
