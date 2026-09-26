# Browser startup fix

run.py now launches Uvicorn as a child process instead of using os.execv.
This keeps the browser-launch thread alive long enough to wait for /health and
open the FastAPI dashboard at http://127.0.0.1:8000/ automatically.

The production dashboard is served directly by FastAPI; Streamlit is not needed
for the production launcher.
