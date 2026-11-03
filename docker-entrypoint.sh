#!/bin/sh
set -e
cd /app/django_project

# SQLite-only fallback: when MYSQL_DATABASE is unset and a db.sqlite3 bind mount is used,
# ensure the WAL sidecars are files (Docker bind mounts create directories when missing).
if [ -z "$MYSQL_DATABASE" ] && [ -e db.sqlite3 ]; then
  for f in db.sqlite3-wal db.sqlite3-shm; do
    if [ -d "$f" ]; then
      echo "Removing mistaken directory $f (expected SQLite WAL sidecar file)."
      rm -rf "$f"
    fi
    if [ ! -e "$f" ]; then
      touch "$f"
    fi
  done
fi

if [ "${RUN_MIGRATIONS}" = "1" ]; then
  echo "RUN_MIGRATIONS=1: executing database migrations..."
  python manage.py migrate --noinput
else
  echo "Skipping migrations (RUN_MIGRATIONS != 1)..."
fi
exec "$@"
