FROM python:3.12-slim

# ffmpeg is the only system dependency — needed for the HEVC transcode.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py r2.py transcode.py scraper.py ./

# All runtime state (session.json, dedup sets, manifest, temp downloads) is
# written relative to the working dir. Run from /app/state so everything lands
# on the mounted volume and survives restarts. Imports still resolve from /app
# (Python adds the script's dir to sys.path).
RUN mkdir -p /app/state
ENV DOWNLOAD_DIR=/app/state/downloads \
    MANIFEST_FILE=/app/state/manifest.jsonl
WORKDIR /app/state

CMD ["python", "/app/watcher.py"]
