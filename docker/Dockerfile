FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# GDAL (pyogrio), GEOS (shapely) and PROJ (pyproj) all ship inside their wheels,
# so no apt packages are needed.
COPY pyproject.toml README.md alembic.ini ./
COPY app ./app
RUN pip install .

# /data is the storage volume; pre-creating it owned by the app user makes fresh named
# volumes writable without running as root.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
USER app

ENV STORAGE_DIR=/data/storage \
    WEB_CONCURRENCY=2
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# The API is a thin HTTP layer (the heavy lifting happens in workers), so 1-2 workers is plenty.
CMD ["sh", "-c", "exec gunicorn app.asgi:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY} -b 0.0.0.0:8000 --graceful-timeout 30 --timeout 120"]
