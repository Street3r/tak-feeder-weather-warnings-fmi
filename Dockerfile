# Stage 1: build
FROM python:3.12-slim AS builder
WORKDIR /app
# git is needed to install python-takserver-api from GitHub
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --prefix=/install .

# Stage 2: runtime
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/Street3r/tak-feeder-weather-warnings-fmi" \
      org.opencontainers.image.description="FMI weather warnings as a TAK mission" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
COPY --from=builder /install /usr/local
# Unprivileged user. The certificate and key must be readable by uid 10001,
# or run the container with user: "0:0" (see README).
RUN useradd --system --uid 10001 --home-dir /data --no-create-home feeder
ENV PYTHONUNBUFFERED=1 \
    HEALTH_FILE=/tmp/tak-feeder-unhealthy
USER 10001
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os, sys; sys.exit(os.path.exists(os.environ['HEALTH_FILE']))"]
ENTRYPOINT ["python", "-m", "tak_feeder_weather_warnings_fmi"]
