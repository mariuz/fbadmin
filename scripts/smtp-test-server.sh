#!/usr/bin/env bash
# A throwaway SMTP server (Mailpit) for the email alert test in tests/test_alerts.py.
#   scripts/smtp-test-server.sh   -> SMTP on 127.0.0.1:1025 (any login accepted), web/API on :8025
#   FBADMIN_TEST_SMTP_HOST=127.0.0.1 FBADMIN_TEST_SMTP_API=http://127.0.0.1:8025 pytest backend/tests/test_alerts.py
set -euo pipefail
IMAGE="${MAILPIT_IMAGE:-axllent/mailpit:latest}"
docker rm -f fbadmin-mailpit >/dev/null 2>&1 || true
docker run -d --name fbadmin-mailpit -p 1025:1025 -p 8025:8025 \
  -e MP_SMTP_AUTH_ACCEPT_ANY=1 -e MP_SMTP_AUTH_ALLOW_INSECURE=1 "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8025/api/v1/info >/dev/null 2>&1 && break
  sleep 1
done
echo "export FBADMIN_TEST_SMTP_HOST=127.0.0.1 FBADMIN_TEST_SMTP_PORT=1025 FBADMIN_TEST_SMTP_API=http://127.0.0.1:8025"
