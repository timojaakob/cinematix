FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The processes run as this user. entrypoint.sh starts as root only to fix
# the ownership of /data (volumes from older versions were written by
# root), then drops to it. PUID/PGID can match it to a host user for bind
# mounts.
RUN useradd --create-home --uid 1000 --user-group cinematix && mkdir /data && chown cinematix:cinematix /data

COPY version.py cinemacity.py storage.py scraper.py webui.py entrypoint.sh ./
COPY templates/ templates/
COPY static/ static/
RUN chmod +x entrypoint.sh

ENV DATA_DIR=/data PORT=5000 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 5000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

ENTRYPOINT ["./entrypoint.sh"]
