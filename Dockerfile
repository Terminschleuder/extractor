# terminschleuder-extractor image.
# Prod runs here; the host venv is only for tests. No GIS/GDAL needed — the
# backend owns the PostGIS Point; we send latitude/longitude as separate floats.
FROM python:3.14-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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
USER extractor

# Run the loop by default. Override with e.g. `--once`, `--dry-run`, `--self-test`.
ENTRYPOINT ["python", "-m", "terminschleuder_extractor"]
CMD []