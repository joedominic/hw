# Docker deployment

ResumeElite runs as **two containers** sharing one image:

| Service | Command | Role |
|---------|---------|------|
| **web** | `runserver` (dev) or `gunicorn` (prod) | HTTP UI + Ninja API; enqueues Huey jobs |
| **huey** | `python manage.py run_huey` | Consumes Redis queue; runs periodic tasks |

Redis is **external** (not in compose). Default host: `192.168.2.174:6379` — set `HUEY_REDIS_*` in `.env`.

## Prerequisites

- Docker Engine + Docker Compose v2
- Reachable Redis on your LAN (or `host.docker.internal` if Redis is on the Docker host)
- `JobApp-Main/.env` — copy from [`.env.example`](../.env.example)

## Quick start (dev)

From the repo root (`JobApp-Main/`):

```bash
cp .env.example .env
# Edit .env: HUEY_REDIS_HOST, SECRET_KEY, ALLOWED_HOSTS

# Avoid Docker creating a *directory* instead of the DB/WAL files on first run:
mkdir -p django_project/media
# Linux/macOS:
touch django_project/db.sqlite3 django_project/db.sqlite3-wal django_project/db.sqlite3-shm
# Windows PowerShell (if you use Docker bind mounts for SQLite):
#   New-Item -ItemType File -Force django_project/db.sqlite3, django_project/db.sqlite3-wal, django_project/db.sqlite3-shm
# If migrate fails with "unable to open database file", check that db.sqlite3-wal and
# db.sqlite3-shm are files, not folders — remove empty directories and recreate as files.

docker compose up --build
```

- UI: http://localhost:8000/
- Huey monitor: http://localhost:8000/jobs/huey/

The entrypoint runs `migrate` on each container start before the main command.

## Production

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Prod overrides:

- **web:** `gunicorn` with 2 workers, 120s timeout
- **Both:** `restart: unless-stopped`, `DEBUG=False` on web/huey
- **Healthchecks:** HTTP probe on web; Redis ping on huey

Set in `.env` before prod:

- `DEBUG=False`
- Strong `SECRET_KEY`
- `ALLOWED_HOSTS` = your public hostname(s)

Serve `/media/` via a reverse proxy (nginx/Caddy). Django `urls.py` does not expose media in production.

## Volumes

| Mount | Purpose |
|-------|---------|
| `./django_project/db.sqlite3` (+ `-wal`, `-shm`) | SQLite database in WAL mode (shared by web + huey) |
| `./django_project/media` | Uploaded resumes and pipeline artifacts |
| `hf_cache` (named volume) | Hugging Face / sentence-transformers model cache |

Back up `db.sqlite3` and `media/` before upgrades.

## External Redis

Containers connect to Redis using `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB` from `.env`.

**Requirements:**

1. Redis listens on an interface reachable from the Docker host (not only `127.0.0.1` on a remote machine, unless you tunnel).
2. Firewall allows port 6379 from the Docker bridge network.
3. `HUEY_IMMEDIATE` is unset or `0` so periodic jobs run in the **huey** container.

### Verify connectivity

```bash
docker compose exec web python -c "import redis; r=redis.Redis(host='192.168.2.174', port=6379); print('PING', r.ping())"
docker compose exec huey python manage.py huey_queue_status
```

Replace `192.168.2.174` with your `HUEY_REDIS_HOST`.

### Redis on the same machine as Docker

If `192.168.2.174` is the Docker host, containers may fail to reach it via the LAN IP (hairpin NAT). Use:

```env
HUEY_REDIS_HOST=host.docker.internal
```

`docker-compose.yml` already adds `extra_hosts: host.docker.internal:host-gateway` for Linux; Docker Desktop provides this on Windows/Mac.

## Huey behavior

- **web** enqueues tasks (optimize, job search, vetting, etc.) to Redis.
- **huey** runs `run_huey` with 2 thread workers and the periodic scheduler.
- Do **not** set `HUEY_IMMEDIATE=1` in Docker — tasks would run in the web process and periodic crons would not schedule.
- Run **one** `huey` replica while using SQLite; multiple workers need PostgreSQL.

Periodic tasks (require huey + Redis):

| Task | Schedule |
|------|----------|
| `enqueue_due_job_search_tasks` | Every minute |
| `mark_stale_job_search_runs_failed` | Every 15 min |
| `enqueue_due_vetting_matching_tasks` | Every 20 min |
| `pipeline_manager` | Every 30 min |
| `cleanup_manager` | Daily 01:30 UTC |

## Smoke test

After `docker compose up --build`:

1. **Redis ping** (see commands above) — expect `PING True`.
2. **Queue status** — `docker compose exec huey python manage.py huey_queue_status` should print queue depths without connection errors.
3. **Huey dashboard** — open `/jobs/huey/`; “immediate mode” should be **off** when Redis is configured.
4. **Enqueue a task** — from the UI, run a job search or trigger “Run now” on a periodic task from the Huey dashboard; confirm the queue depth changes and the task completes (check logs: `docker compose logs -f huey`).

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Jobs stuck on “Queued” | `docker compose ps` — is **huey** running? Redis ping? |
| `Connection refused` to Redis | `HUEY_REDIS_HOST`, firewall, bind address on Redis server |
| `database is locked` | SQLite contention — reduce parallel Huey work or migrate to PostgreSQL |
| `database disk image is malformed` | WAL files out of sync — stop web/huey, mount all three DB files in compose (see Volumes), recover from backup |
| First job search slow | Model download — `hf_cache` volume persists `all-MiniLM-L6-v2` after first run |
| Large image build | ~2–3 GB content (CPU torch + sentence-transformers on slim base) |

## Image notes

- Base: `python:3.12-slim-bookworm`; CPU-only PyTorch from `download.pytorch.org/whl/cpu`
- `playwright` / `altair` / `GitPython` omitted from `requirements.txt` (unused by the Django app)
- Build context: repo root (`JobApp-Main/`)
- Compose `env_file: .env` injects variables into the container environment (optional file; copy from `.env.example`)

## Follow-ups (not in v1)

- PostgreSQL instead of SQLite for multi-worker / HA
- Redis AUTH/TLS for untrusted networks
- Separate slim image without torch if embeddings move to a dedicated worker
