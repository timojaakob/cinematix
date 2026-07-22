#!/bin/bash
set -e
python -u scraper.py &
python -u webui.py &
wait -n
exit $?
