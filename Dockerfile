# Rescan API. Tika runs as a separate service; see docker-compose.yml.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# poppler-utils rasterises scanned PDFs for the OCR fallback; without it and
# tesseract, scanned documents are dead-lettered to manual review rather than
# silently returning empty text.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY rescan ./rescan
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 rescan \
    && mkdir -p /data && chown -R rescan:rescan /data /app
USER rescan

ENV RESCAN_DATA_DIR=/data \
    RESCAN_DB_PATH=/data/rescan.db \
    RESCAN_UPLOAD_DIR=/data/uploads \
    RESCAN_TIKA_URL=http://tika:9998

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "rescan.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
