#!/bin/sh
set -e
cd /app/django_project

# Docker bind-mounts create directories when db.sqlite3-wal/shm are missing on the host.
# SQLite WAL mode requires these paths to be files, not folders.
for f in db.sqlite3-wal db.sqlite3-shm; do
  if [ -d "$f" ]; then
    echo "Removing mistaken directory $f (expected SQLite WAL sidecar file)."
    rm -rf "$f"
  fi
  if [ ! -e "$f" ]; then
    touch "$f"
  fi
done

python manage.py migrate --noinput
exec "$@"
