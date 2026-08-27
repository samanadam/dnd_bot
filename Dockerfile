FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    WHISPER_CACHE_DIR=/models \
    HF_HOME=/models \
    XDG_CACHE_HOME=/models

# ffmpeg is required for voice receive and for mp3 output.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dnd_bot/ ./dnd_bot/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# Never run as root.
RUN useradd --create-home --uid 10001 dndbot \
    && mkdir -p /data /models \
    && chown -R dndbot:dndbot /app /data /models
USER dndbot

VOLUME ["/data", "/models"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["python", "-m", "dnd_bot"]
