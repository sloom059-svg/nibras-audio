FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu124 && \
    pip3 install --no-cache-dir -r requirements.txt

COPY handler.py .
CMD ["python3","-u","handler.py"]
