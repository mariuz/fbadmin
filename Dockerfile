# --- build the web UI -------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build   # writes to /src/backend/fbadmin/static

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FBADMIN_HOST=0.0.0.0 FBADMIN_PORT=5050 FBADMIN_DATA_DIR=/data
WORKDIR /app
COPY backend/ ./backend/
COPY --from=ui /src/backend/fbadmin/static ./backend/fbadmin/static
RUN pip install --no-cache-dir ./backend && useradd -r -u 1000 fbadmin && mkdir /data && chown fbadmin /data
USER fbadmin
VOLUME /data
EXPOSE 5050
CMD ["fbadmin", "--no-browser"]
