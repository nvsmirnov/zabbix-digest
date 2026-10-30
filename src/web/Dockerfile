# Build context: repository root (the image needs common/ and web/).
#   docker build -f src/web/Dockerfile .
# Build arguments (all optional):
#   BASE_IMAGE_PYTHON  base image of the runtime stage; must provide python3 with the venv
#                      module (Debian/Ubuntu: python3-venv), or BUILD_EXTRA_CMD must install it.
#   BUILD_EXTRA_CMD    shell command run as root right after FROM of the runtime stage,
#                      e.g. to install Python or set up a proxy or CA certificates. Skipped when empty.
#   APP_UID, APP_GID   numeric user and group of the service (default 10001, 10001).
#   BASE_IMAGE_NODE    base image of the UI build stage; must provide node and npm.
#   BUILD_EXTRA_CMD_UI shell command run as root right after FROM of the UI build stage. Skipped when empty.
ARG BASE_IMAGE_NODE=node:22-slim
ARG BASE_IMAGE_PYTHON=python:3.12-slim

FROM ${BASE_IMAGE_NODE} AS ui
ARG BUILD_EXTRA_CMD_UI=""
RUN if [ -n "$BUILD_EXTRA_CMD_UI" ]; then sh -c "$BUILD_EXTRA_CMD_UI"; fi
WORKDIR /ui
COPY src/web/ui/package.json src/web/ui/package-lock.json /ui/
RUN npm ci
COPY src/web/ui /ui
RUN npm run build

FROM ${BASE_IMAGE_PYTHON}

ARG BUILD_EXTRA_CMD=""
RUN if [ -n "$BUILD_EXTRA_CMD" ]; then sh -c "$BUILD_EXTRA_CMD"; fi

# A virtual environment works with any base image, including distributions that
# forbid system-wide pip installs (PEP 668, "externally-managed-environment").
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/common/src:/app/web/src \
    WEB_CONFIG=/config/web.yaml \
    STATIC_DIR=/app/ui

WORKDIR /app
COPY src/common/requirements.txt /app/common/requirements.txt
COPY src/web/requirements.txt /app/web/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/web/requirements.txt

COPY src/common/src /app/common/src
COPY src/web/src /app/web/src
COPY --from=ui /ui/dist /app/ui

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
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "web"]
