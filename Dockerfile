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

# --- Firebird 5 client library (native driver) ------------------------------
# The pinned, SHA-256 checked official release for the target architecture; only the client
# files (libfbclient, the ChaCha wire encryption plugin and libtomcrypt, messages): about 5 MB.
# Extracting needs no target-architecture code, so it runs on the build host's platform.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS fbclient
ARG TARGETARCH
COPY packaging/embedded/ /src/packaging/embedded/
RUN python /src/packaging/embedded/fetch-embedded.py --client-only --system-libtommath \
      --platform "linux-$([ "$TARGETARCH" = arm64 ] && echo arm64 || echo x64)" --dest /opt/firebird

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
# The client library is on the system library path, so the default driver setting (auto) finds it
# and uses the native driver; FBADMIN_DRIVER=firebirdsql selects the pure Python driver. The client
# needs libtommath, and ICU for time zone names (Debian 13's libicu76, about 37 MB; without it
# auto would choose the pure Python driver).
COPY --from=fbclient /opt/firebird /opt/firebird
RUN apt-get update && apt-get install -y --no-install-recommends libtommath1 libicu76 \
    && rm -rf /var/lib/apt/lists/* \
    && echo /opt/firebird/lib > /etc/ld.so.conf.d/firebird.conf && ldconfig
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
