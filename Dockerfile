FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0

EXPOSE 10000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -fsS http://127.0.0.1:${PORT:-10000}/health || exit 1

CMD ["sh", "-c", "gunicorn api.main:APP --worker-class uvicorn.workers.UvicornWorker --workers 1 --bind 0.0.0.0:${PORT:-10000} --timeout 60 --access-logfile -"]
