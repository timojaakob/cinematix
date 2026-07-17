FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py webui.py storage.py .
COPY templates/ templates/

CMD ["python", "-u", "scraper.py"]
