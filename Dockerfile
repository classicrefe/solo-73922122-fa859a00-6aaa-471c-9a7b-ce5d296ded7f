FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/catalog.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4); \
sys.exit(0 if r.status == 200 else 1)"

# Single worker + threads keeps SQLite writes trivially serialised;
# busy_timeout + WAL are enabled in app/db.py.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
