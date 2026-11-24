# Local development profiles

HireEdge reads `APP_ENV`. When it is unset, the value is `development`: `DEBUG` defaults to on, production startup guards stay off, and a checkout with no extra services can still boot.

## Tiers

| Tier | Command | What it needs |
| :--- | :--- | :--- |
| Minimal | `.\scripts\dev.ps1 web -Minimal` | The `env` virtualenv. Tasks run inside the web process (`HUEY_IMMEDIATE=1`). SQLite and local `media/` are enough. |
| Standard | `.\scripts\dev.ps1 web` and, in another terminal, `.\scripts\dev.ps1 worker` | `.env` as today: MariaDB or SQLite, Redis (port 6380 in `.env.example`), local `media/`. |
| Production parity | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build` | `APP_ENV=production` in that compose file. Object storage and a shared Redis are required or the process refuses to start. |

`.\scripts\dev.ps1 doctor` prints the active profile, database, cache, and storage backend.

`.\scripts\dev.ps1 reap` marks optimizer and job-search runs as failed when their worker was stopped. Periodic reaping does not run while `HUEY_IMMEDIATE=1`.

## Guards

`APP_ENV=production` refuses to start without a Redis cache, an object-storage bucket, and `FERNET_KEYS`, unless the matching `ALLOW_*` flag is set. `DEBUG=False` on its own does not enable those guards, so local debugging of production static files still works.
