# syntax=docker/dockerfile:1.7
# One image, two roles: API (default CMD) and worker (`python -m app.workers.main`).
# Same code + same dependencies => a message contract can never drift between them.

############################
# Stage 1: build wheels
############################
FROM python:3.15-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
# Build into an isolated virtualenv that is copied into the runtime stage as-is.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

############################
# Stage 2: runtime
############################
FROM python:3.15-slim-bookworm AS runtime

ARG APP_VERSION=dev
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} \
    FORWARDED_ALLOW_IPS=127.0.0.1

LABEL org.opencontainers.image.title="task-platform" \
      org.opencontainers.image.version="${APP_VERSION}"

# Non-root user: a compromised process can't modify the image or bind privileged ports.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./

# Deployment bundle: compose files, Nginx/monitoring/service configs and scripts versioned
# together with the code. EC2 hosts extract it from the image, so "deploy image X" also
# means "deploy the exact configuration of X" — and rollback restores both.
COPY deploy /opt/bundle/deploy
COPY nginx /opt/bundle/nginx
COPY monitoring /opt/bundle/monitoring
COPY config /opt/bundle/config

USER app
EXPOSE 8000

# Liveness only (no dependency checks) — see app/api/routes/health.py for why.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly for graceful shutdown.
# --timeout-keep-alive 75 > Nginx upstream keepalive_timeout (60s): the proxy always closes
# idle connections first, avoiding sporadic 502s from reusing a connection uvicorn closed.
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --timeout-keep-alive 75 --timeout-graceful-shutdown 20 --no-server-header"]
