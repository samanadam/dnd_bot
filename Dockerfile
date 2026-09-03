FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

# ffmpeg encodes the finished session to Opus; libopus is for voice receive.
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
    && mkdir -p /data \
    && chown -R dndbot:dndbot /app /data
USER dndbot

VOLUME ["/data"]

# No model download any more, so the bot is ready in seconds.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["python", "-m", "dnd_bot"]
