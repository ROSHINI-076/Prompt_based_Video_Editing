# ── Build stage ────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Install ffmpeg, curl and runtime deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml ./
COPY incubrix ./incubrix
RUN pip install --no-cache-dir -e '.[dev]'

COPY . .

# Default command (overridden in compose)
CMD ["uvicorn", "incubrix.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
