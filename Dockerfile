# Multi-arch (linux/amd64, linux/arm64) image; published to ghcr.io/mariuz/fbadmin by
# .github/workflows/docker.yml. Build locally with: docker build -t fbadmin .

# --- build the web UI -------------------------------------------------------
# The UI is plain JS/CSS, so it is built once on the build host's platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build   # writes to /src/backend/fbadmin/static

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="fbadmin" \
      org.opencontainers.image.description="pgAdmin-style web administration tool for Firebird SQL" \
      org.opencontainers.image.source="https://github.com/mariuz/fbadmin" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FBADMIN_HOST=0.0.0.0 FBADMIN_PORT=5050 FBADMIN_DATA_DIR=/data
WORKDIR /app
COPY backend/ ./backend/
COPY --from=ui /src/backend/fbadmin/static ./backend/fbadmin/static
RUN pip install --no-cache-dir ./backend && useradd -r -u 1000 fbadmin && mkdir /data && chown fbadmin /data
USER fbadmin
VOLUME /data
EXPOSE 5050
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('FBADMIN_PORT', '5050'), timeout=4)"]
CMD ["fbadmin", "--no-browser"]
