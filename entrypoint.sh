#!/bin/bash
set -e

if [ "$(id -u)" = "0" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  if [ "$(id -g cinematix)" != "$PGID" ]; then groupmod -o -g "$PGID" cinematix; fi
  if [ "$(id -u cinematix)" != "$PUID" ]; then usermod -o -u "$PUID" cinematix; fi
  chown -R cinematix:cinematix "$DATA_DIR"
  exec setpriv --reuid=cinematix --regid=cinematix --init-groups "$0" "$@"
fi

# The scraper and the web UI run side by side; if either exits, the
# container exits and Docker's restart policy brings both back.
python -u scraper.py &
python -u webui.py &
wait -n
exit $?
