FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs npm ca-certificates curl git build-essential && rm -rf /var/lib/apt/lists/*
RUN if [ -x /usr/bin/nodejs ] && [ ! -e /usr/bin/node ]; then ln -s /usr/bin/nodejs /usr/bin/node; fi && node --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
ENV TORCH_HOME=/root/.cache/torch
RUN mkdir -p /root/.cache/torch/hub/checkpoints &&     curl -L --fail --retry 5 --retry-delay 3     https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th     -o /root/.cache/torch/hub/checkpoints/955717e8-8726e21a.th
COPY . .
ENV OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
ENV PYTHONUNBUFFERED=1
CMD ["sh","-c","gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 1 --timeout 1800 bot:app"]
