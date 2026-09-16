FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

# ffmpeg encodes the finished session to Opus; libopus is for voice receive.
# git only installs the pinned py-cord commit (see requirements.txt).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dev.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp only, and only on request: it needs updating far more often than
# anything else in this image, and music over R2 does not need it at all.
ARG WITH_YTDLP=false
RUN if [ "$WITH_YTDLP" = "true" ]; then \
        pip install --no-cache-dir -r requirements-optional.txt; \
    fi

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

# The portal API, when API_ENABLED=true. Documentation only - compose publishes
# this to the host's loopback, where a reverse proxy terminates TLS.
EXPOSE 8080

# Still the heartbeat file, not the HTTP API: recording liveness is what should
# decide a restart, and a bot that records perfectly with a dead API must not be
# killed for it.
# No model download any more, so the bot is ready in seconds.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["python", "-m", "dnd_bot"]
