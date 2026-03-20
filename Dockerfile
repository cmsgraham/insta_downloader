FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    chromium \
    chromium-driver \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir bgutil-ytdlp-pot-provider

COPY web_app.py cookie_manager.py gunicorn.conf.py ./

RUN mkdir -p /app/downloads /app/cookie_data

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "web_app:app"]
