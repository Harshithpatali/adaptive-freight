from __future__ import annotations
import os, sys, time, subprocess, threading, webbrowser, socket
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
HOST = os.getenv("APP_HOST", "127.0.0.1")
REQUESTED_PORT = int(os.getenv("APP_PORT", "8000"))

def make_url(host: str, port: int, path: str = "") -> str:
    return f"http://{host}:{port}{path}"

def http_ok(url: str, timeout: float = 0.8) -> bool:
    try:
        r = requests.get(url, timeout=timeout)
        return r.ok
    except requests.RequestException:
        return False

def port_is_free(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        return sock.connect_ex((host, port)) != 0
    finally:
        sock.close()

def choose_port(host: str, requested_port: int) -> tuple[int, bool]:
    if http_ok(make_url(host, requested_port, "/health"), timeout=0.8):
        return requested_port, True
    for port in range(requested_port, requested_port + 20):
        if port_is_free(host, port):
            return port, False
    raise RuntimeError(f"No free port found in {requested_port}-{requested_port + 19}")

def wait_healthy(url: str, attempts: int = 120) -> bool:
    for _ in range(attempts):
        if http_ok(url + "/health", timeout=0.8):
            return True
        time.sleep(0.25)
    return False

def open_dashboard(url: str) -> None:
    if wait_healthy(url):
        time.sleep(0.2)
        try:
            opened = webbrowser.open_new_tab(url + "/")
            print(f"Dashboard: {url}/")
            print("Browser launch requested." if opened else "Browser launch was not handled automatically; open the URL above.")
        except Exception as exc:
            print(f"Dashboard: {url}/")
            print(f"Browser could not be opened automatically: {exc}")
    else:
        print(f"Backend did not become healthy. Open {url}/ manually if the server is still running.")

if __name__ == "__main__":
    port, reused = choose_port(HOST, REQUESTED_PORT)
    URL = make_url(HOST, port)
    print(f"Adaptive Freight: requested port={REQUESTED_PORT}, selected port={port}")
    if reused:
        print(f"Existing healthy backend detected on {URL}")
        print("Reusing existing backend; no second server will be started.")
        open_dashboard(URL)
        raise SystemExit(0)

    print(f"Starting Adaptive Freight backend + live dashboard at {URL}")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:APP", "--host", HOST, "--port", str(port)],
        cwd=str(ROOT),
    )
    browser_thread = threading.Thread(target=open_dashboard, args=(URL,), daemon=True)
    browser_thread.start()
    try:
        return_code = server.wait()
    except KeyboardInterrupt:
        print("Stopping Adaptive Freight...")
        server.terminate()
        try:
            return_code = server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            server.kill()
            return_code = server.wait()
    raise SystemExit(return_code)
