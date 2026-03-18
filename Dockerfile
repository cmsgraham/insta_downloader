FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web_app.py gunicorn.conf.py ./

RUN mkdir -p /app/downloads

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "web_app:app"]
