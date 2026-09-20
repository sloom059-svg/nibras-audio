FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/*\nRUN if [ -x /usr/bin/nodejs ] && [ ! -e /usr/bin/node ]; then ln -s /usr/bin/nodejs /usr/bin/node; fi && node --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["sh","-c","gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 1 --timeout 1800 bot:app"]
