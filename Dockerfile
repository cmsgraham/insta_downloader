FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web_app.py gunicorn.conf.py ./

RUN mkdir -p /app/downloads

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "web_app:app"]
