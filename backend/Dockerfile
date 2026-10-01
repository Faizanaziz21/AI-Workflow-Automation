# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    FF_STORAGE_DIR=/data/storage
RUN groupadd --system --gid 10001 flowforge && useradd --system --uid 10001 --gid flowforge --home /app flowforge \
    && mkdir -p /data/storage && chown -R flowforge:flowforge /data
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels
COPY --chown=flowforge:flowforge alembic.ini ./
COPY --chown=flowforge:flowforge migrations ./migrations
COPY --chown=flowforge:flowforge app ./app
USER flowforge
EXPOSE 8000 9101
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"
# Role is chosen by command: api (default), worker, scheduler, migrate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--workers", "1"]
