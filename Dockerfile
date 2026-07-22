FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py webui.py storage.py entrypoint.sh .
COPY templates/ templates/
COPY static/ static/
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
