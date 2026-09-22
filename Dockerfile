FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# run as an unprivileged user; the bridge only needs to write its SQLite file
RUN useradd --create-home --uid 10001 bridge \
    && mkdir -p /data && chown bridge /data
USER bridge
VOLUME /data

ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bridge.sqlite3
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
