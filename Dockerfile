# terminschleuder-extractor image.
# Prod runs here; the host venv is only for tests. No GIS/GDAL needed — the
# backend owns the PostGIS Point; we send latitude/longitude as separate floats.
FROM python:3.14-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install runtime deps first for layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# App source.
COPY src/ ./src/

# Non-root user for the long-running process.
RUN useradd --create-home --uid 1001 extractor \
    && mkdir -p /app/state \
    && chown -R extractor:extractor /app

# OCI image metadata. CI (docker/metadata-action) appends source/revision/version.
LABEL org.opencontainers.image.title="terminschleuder-extractor" \
      org.opencontainers.image.description="LLM-based extractor for the terminschleuder ingestion API (OpenAI-compatible inference)" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="docker.io/library/python:3.14-slim-bookworm"

USER extractor

# Run the loop by default. Override with e.g. `--once`, `--dry-run`, `--self-test`.
ENTRYPOINT ["python", "-m", "terminschleuder_extractor"]
CMD []