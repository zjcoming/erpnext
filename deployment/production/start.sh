#!/usr/bin/env bash
set -Eeuo pipefail

gunicorn_workers="${GUNICORN_WORKERS:-2}"
gunicorn_threads="${GUNICORN_THREADS:-4}"
gunicorn_timeout="${GUNICORN_TIMEOUT:-120}"
gunicorn_max_requests="${GUNICORN_MAX_REQUESTS:-2000}"
gunicorn_max_requests_jitter="${GUNICORN_MAX_REQUESTS_JITTER:-200}"

echo "Starting Gunicorn: workers=${gunicorn_workers}, threads=${gunicorn_threads}, timeout=${gunicorn_timeout}s"

exec /home/frappe/frappe-bench/env/bin/gunicorn \
  --chdir=/home/frappe/frappe-bench/sites \
  --bind=0.0.0.0:8000 \
  --threads="${gunicorn_threads}" \
  --workers="${gunicorn_workers}" \
  --worker-class=gthread \
  --worker-tmp-dir=/dev/shm \
  --timeout="${gunicorn_timeout}" \
  --max-requests="${gunicorn_max_requests}" \
  --max-requests-jitter="${gunicorn_max_requests_jitter}" \
  --error-logfile=- \
  --capture-output \
  --preload \
  frappe.app:application
