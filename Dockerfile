FROM python:3.12-slim-bookworm

# Install system dependencies required for OpenCV, PyAV (FFmpeg), and health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    curl \
    openssl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies first for optimal Docker layer caching
COPY requirements.txt pyproject.toml /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY momcozy /app/momcozy
COPY momcozy.py /app/momcozy.py
COPY README.md /app/README.md

# Install package so `momcozy` command line is ready
RUN pip install --no-cache-dir -e .

# Create directory for persistent configuration
RUN mkdir -p /config

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    MOMCOZY_CONFIG=/config/.momcozy.json

# Expose web dashboard (8421) and Chromecast LAN streaming port (8420)
EXPOSE 8420 8421

# Health check to ensure the dashboard status API is responding (supports HTTP and HTTPS)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f -k http://localhost:8421/api/status || curl -f -k https://localhost:8421/api/status || exit 1

# Default entrypoint and command: run web server on 0.0.0.0:8421
ENTRYPOINT ["python3", "/app/momcozy.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8421"]
