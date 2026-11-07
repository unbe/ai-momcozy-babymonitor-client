FROM python:3.15-slim-trixie

# Install system dependencies required for OpenCV, PyAV (FFmpeg), VA-API hardware acceleration, and health checks.
# Note: i965-va-driver-shaders (amd64 non-free) provides encoding shaders for Intel Haswell–Coffee Lake GPUs.
RUN export DEBIAN_FRONTEND=noninteractive \
 && sed -i 's/Components: main/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && EXTRA_PKGS="" \
 && if [ "$(dpkg --print-architecture)" = "amd64" ]; then EXTRA_PKGS="i965-va-driver-shaders"; fi \
 && apt-get install -y --no-install-recommends \
    ffmpeg \
    va-driver-all \
    vainfo \
    procps \
    libgl1 \
    libglib2.0-0t64 \
    ca-certificates \
    curl \
    openssl \
    $EXTRA_PKGS \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies first for optimal Docker layer caching
COPY pyproject.toml /app/
RUN pip install --no-cache-dir --only-deps .

# Copy application source
COPY momcozy /app/momcozy
COPY momcozy.py /app/momcozy.py
COPY README.md /app/README.md

# Install package so `momcozy` command line is ready
RUN pip install --no-cache-dir --no-deps -e .

# Create directory for persistent configuration and timelapse recordings
RUN mkdir -p /config /timelapse && ln -s /timelapse /app/timelapse

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    AIOHTTP_NOSENDFILE=1 \
    MOMCOZY_CONFIG=/config/.momcozy.json \
    MOMCOZY_TIMELAPSE_DIR=/timelapse

# Declare timelapse storage volume for persistence across container rebuilds/restarts
VOLUME ["/timelapse"]

# Expose web dashboard (8421) and Chromecast LAN streaming port (8420)
EXPOSE 8420 8421

# Health check to ensure the server is responding (supports HTTP and HTTPS) without external requests
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f -k http://localhost:8421/health || curl -f -k https://localhost:8421/health || exit 1

# Default entrypoint and command: run web server on 0.0.0.0:8421
ENTRYPOINT ["python3", "/app/momcozy.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8421"]
