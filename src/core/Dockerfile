# Build context: repository root (the image needs common/ and core/).
#   docker build -f src/core/Dockerfile .
# Build arguments (all optional):
#   BASE_IMAGE_PYTHON  base image of the runtime stage; must provide python3 with the venv
#                      module (Debian/Ubuntu: python3-venv), or BUILD_EXTRA_CMD must install it.
#   BUILD_EXTRA_CMD    shell command run as root right after FROM of the runtime stage,
#                      e.g. to install Python or set up a proxy or CA certificates. Skipped when empty.
#   APP_UID, APP_GID   numeric user and group of the service (default 10001, 10001).
ARG BASE_IMAGE_PYTHON=python:3.12-slim

FROM ${BASE_IMAGE_PYTHON}

ARG BUILD_EXTRA_CMD=""
RUN if [ -n "$BUILD_EXTRA_CMD" ]; then sh -c "$BUILD_EXTRA_CMD"; fi

# A virtual environment works with any base image, including distributions that
# forbid system-wide pip installs (PEP 668, "externally-managed-environment").
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/common/src:/app/core/src \
    CORE_CONFIG=/config/core.yaml

WORKDIR /app
COPY src/common/requirements.txt /app/common/requirements.txt
COPY src/core/requirements.txt /app/core/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/core/requirements.txt

COPY src/common/src /app/common/src
COPY src/core/src /app/core/src

# Fixed numeric UID and GID: Kubernetes runAsNonRoot can check only a numeric USER,
# and volume access is usually granted to a group (fsGroup).
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid "$APP_GID" zabbix-digest \
    && useradd --system --uid "$APP_UID" --gid "$APP_GID" --home-dir /data --no-create-home zabbix-digest \
    && mkdir -p /data /config \
    && chown "$APP_UID:$APP_GID" /data \
    && chmod 0770 /data
USER ${APP_UID}:${APP_GID}
VOLUME ["/data"]
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health/live', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "core"]
CMD ["run"]
