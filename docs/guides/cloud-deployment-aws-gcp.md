# Cloud deployment: AWS and GCP

HireEdge is one container image with two long-running processes (web and Huey) plus a one-shot migration. Production code expects **MySQL or MariaDB**, **Redis**, and **S3-compatible object storage**. This guide picks concrete services and the order to turn them on.

Related: [Container scaling](container-scaling-and-deployment.md), [Docker](../DOCKER.md), [Production hardening](production-hardening.md), [Statelessness audit](../architecture/statelessness-and-horizontal-scaling-audit.md).

## Decision

| If you… | Use | Why |
| --- | --- | --- |
| Have no cloud preference | **AWS** (below) | The app already uses PyMySQL and `django-storages` S3. ECS runs a non-HTTP worker cleanly. |
| Are already on GCP | **GKE Autopilot + Cloud SQL for MySQL** | Same data plane. Cloud Run is a bad fit for Huey. |
| Want the smallest first bill | **One VM + `docker-compose.prod.yml`**, managed MySQL and Redis | Same image and env. Move to ECS or GKE when you need a second web replica. |
| Want Postgres | Do not switch yet | There is no Postgres `DATABASES` branch. Backups call `mysqldump`. |

Do not run SQLite, LocMem cache, or local media once more than one container serves traffic. `APP_ENV=production` refuses those unless an explicit `ALLOW_*` override is set. Leave the overrides unset.

## What has to be running

| Process | Command | Notes |
| --- | --- | --- |
| Web | `gunicorn core.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 120 --graceful-timeout 30` | Port **8000**. Static files come from WhiteNoise (`collectstatic` at image build). |
| Huey | `python manage.py run_huey` | `HUEY_WORKERS` defaults to **2 threads**. Stop with **SIGINT** and a grace period of at least **10 minutes** so in-flight scrapes and optimizations can finish. |
| Migrate | `python manage.py migrate --noinput` | Once per deploy. Set `RUN_MIGRATIONS=0` on web and Huey. |

Health checks (no login):

- Liveness: `GET /health/live` (alias `GET /health/`)
- Readiness: `GET /health/ready` (database `SELECT 1` and a cache read/write; 503 when either fails)

`/media/` must stay on Django. A bucket or nginx alias in front of `MEDIA_ROOT` skips the owner check. With `AWS_STORAGE_BUCKET_NAME` set, Django redirects to a presigned URL (`AWS_QUERYSTRING_EXPIRE`, default 120 seconds).

Periodic Huey tasks (search, vetting, pipeline, cleanup) elect a leader with Redis locks. Multiple Huey replicas are fine. You do not need a separate cron for those tasks. Database backups are the exception: use the managed database backup, and optionally `ops/backup.sh`.

## AWS (preferred)

### Services

| Concern | Service | Choice |
| --- | --- | --- |
| Image | ECR | One repository. Web and Huey use the same tag. |
| Web | ECS Fargate service | Application Load Balancer, HTTPS (ACM), target group health check `GET /health/ready` on port 8000. |
| Worker | ECS Fargate service | No load balancer. `stopTimeout` 600 seconds. Desired count 1 until queue depth requires more. |
| Migrate | ECS one-off task | Run and wait before shifting the web service to the new task definition. |
| Database | RDS for MySQL 8 | Multi-AZ when you have paying users. `utf8mb4`. Start around `db.t4g.medium`, 100 GB gp3. Automated backups, 7–14 day retention. |
| Queue Redis | ElastiCache Redis | `noeviction`. This is Huey (`HUEY_REDIS_DB=0`). |
| Cache Redis | ElastiCache Redis | `allkeys-lru`. Django cache (`DJANGO_CACHE_REDIS_DB=1` or a separate URL). Do not share the queue instance once embeddings and rate limits are busy (audit S-05). |
| Media | S3 | Private bucket, block public access. ECS task role with `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on that bucket. Leave access keys empty so boto3 uses the task role. |
| Secrets | Secrets Manager | Inject as environment variables. See the required list below. |
| Email | SES | SMTP into `EMAIL_HOST_*`. Verify the sending domain. |
| Logs | CloudWatch Logs | `awslogs` driver on both services. |
| DNS / TLS | Route 53 + ACM | Certificate on the ALB, not inside the container. |
| Pooler | RDS Proxy | Add when `(web tasks × 2) + (huey tasks × HUEY_WORKERS)` approaches `max_connections` (audit S-04). Not required for a single web task and a single worker. |

Region: one region close to users. LLM calls leave that region anyway.

### Network

- Tasks in private subnets.
- ALB in public subnets.
- RDS and ElastiCache in private subnets. Security groups: web and Huey to MySQL 3306 and Redis 6379 only.
- NAT (or VPC endpoints for S3, ECR, Secrets Manager, and CloudWatch) so tasks can pull images and call Stripe and LLM APIs.
- Set `TRUSTED_PROXY_COUNT=1` so the ALB hop is the only trusted forwarded-for address. Leave it at 0 and the app ignores `X-Forwarded-For`, which is safer than trusting a spoofed header and wrong for rate limits behind the ALB.

### GCP (alternative)

| Concern | Service | Choice |
| --- | --- | --- |
| Web | GKE Autopilot Deployment | Ingress or Gateway, backend health `GET /health/ready`. |
| Worker | GKE Autopilot Deployment | No Service. `terminationGracePeriodSeconds: 600`. |
| Migrate | Kubernetes Job | `RUN_MIGRATIONS=0` on the Deployments. |
| Database | Cloud SQL for **MySQL** | Not Cloud SQL for PostgreSQL. Private IP. Automated backups on. |
| Redis | Memorystore for Redis, two instances | Queue: no eviction. Cache: LRU. |
| Media | Cloud Storage | Enable the [S3 interoperability / HMAC](https://cloud.google.com/storage/docs/interoperability) endpoint and set `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` to the HMAC key. The storage backend is boto3, not the native GCS client. |
| Secrets | Secret Manager | Mounted or synced into the pod env. |
| Email | Any SMTP (or a transactional provider) | There is no Gmail API integration. |
| Build | Cloud Build or GitHub Actions | Push the same Dockerfile to Artifact Registry. |

Cloud Run can host the web process if you set minimum instances and a request timeout of at least 120 seconds. It should not host Huey: the worker does not listen on a port, and scale-to-zero drops the queue consumer. If you use Cloud Run for web, run Huey on GKE or a Compute Engine VM that shares Cloud SQL, Memorystore, and the bucket.

### Single-VM starter (either cloud)

One EC2 (`t3.large` or larger; the image includes CPU PyTorch) or one GCE `e2-standard-4`, Docker, and `docker-compose.prod.yml`. Point `MYSQL_HOST` and Redis at the managed services, and set the S3 variables. This matches how the repo is already wired. Split web and worker onto ECS or GKE before you scale the web service past one task.

## Required environment

Set `APP_ENV=production`. Boot fails closed if the following are missing or left on insecure defaults:

| Variable | Value |
| --- | --- |
| `SECRET_KEY` | Long random string. Not the `django-insecure-` default. |
| `DEBUG` | `False`. Cookie and HSTS defaults follow `DEBUG`, not `APP_ENV`. |
| `ALLOWED_HOSTS` | Your public host name. |
| `CSRF_TRUSTED_ORIGINS` | `https://` plus that host. |
| `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT` | RDS or Cloud SQL endpoint. |
| `DJANGO_CACHE_USE_REDIS` | `1`, plus `DJANGO_CACHE_URL` or host and `DJANGO_CACHE_REDIS_DB`. |
| `HUEY_REDIS_HOST`, `HUEY_REDIS_PORT`, `HUEY_REDIS_DB` | Queue Redis. `HUEY_NAME` must match on web and worker. |
| `REDIS_PASSWORD` | Set. Prefer `REDIS_USE_TLS=1` on ElastiCache and Memorystore in-transit encryption. |
| `AWS_STORAGE_BUCKET_NAME` | Bucket name. Region in `AWS_S3_REGION_NAME`. |
| `FERNET_KEYS` | URL-safe Fernet key. Do not rely on a key derived from `SECRET_KEY` (`ALLOW_DERIVED_FERNET_KEY` stays unset). Rotating this key makes stored BYOK API keys unreadable. |
| `EMAIL_BACKEND` | An SMTP backend, not console. |
| `STRIPE_WEBHOOK_SECRET` | Required whenever `STRIPE_SECRET_KEY` is set. |
| `RUN_MIGRATIONS` | `0` on web and Huey. |
| `HUEY_IMMEDIATE` | `0` or unset. |

Also set, or the product is up but incomplete:

- `SECURE_SSL_REDIRECT=1`, `SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1` (defaults already do this when `APP_ENV=production`, including if `DEBUG` was left on).
- `ACCOUNT_EMAIL_BASE_URL` to the public `https` origin (verification and password reset links).
- `SIGNUP_ENABLED` and `REQUIRE_EMAIL_VERIFICATION` to match how you want accounts opened.
- LLM keys you intend to use as platform keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`.
- Stripe: `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`. Point the Stripe endpoint at `https://<host>/billing/stripe/webhook/`.
- Optional telemetry: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`.

Leave unset in a shared production cluster:

- `ALLOW_SQLITE_IN_PRODUCTION`, `ALLOW_LOCMEM_CACHE_IN_PRODUCTION`, `ALLOW_LOCAL_MEDIA_IN_PRODUCTION`, `ALLOW_CONSOLE_EMAIL_IN_PRODUCTION`, `ALLOW_EMPTY_STRIPE_WEBHOOK_SECRET`, `ALLOW_DERIVED_FERNET_KEY`.
- Tenant-controlled Ollama Local hosts. Prefer cloud models, or one private Ollama URL you set with `OLLAMA_LOCAL_HOST` and do not let customers override. See the security review: a user-supplied base URL is fetched by the server.

`MYSQL_CONN_MAX_AGE` defaults to 60 seconds. Example budget at the recommended start size: 1 web task × 2 workers + 1 Huey task × 2 threads = 4 persistent connections.

## Rollout order

1. Create the database, both Redis instances, and the private bucket. Apply the schema user from your existing MySQL provisioning notes (`ops/` and `.env.example`).
2. Build the Dockerfile (context is the repo root) and push it. The image is `python:3.12-slim-bookworm` plus `requirements.txt`.
3. Store secrets. Confirm `APP_ENV=production` and `DEBUG=False` on every task.
4. Run the migrate task. Confirm it exits 0.
5. Start Huey, then web. Ready check must return 200 before the load balancer sends traffic.
6. Create a superuser or staff user with `set_staff_user` from a one-off task. Confirm signup, email, a resume upload (object lands in the bucket), and a short job search (Huey log shows the task).
7. Register the Stripe webhook only after the signature secret is in the environment.
8. Turn on database automated backups and a restore drill before inviting customers. Application-level dumps in `docs/guides/disaster-recovery-and-backups.md` are MySQL dumps; they complement RDS or Cloud SQL backups, they do not replace them.

## Scaling notes already known

These are existing audit findings, not new cloud limits:

- **S-01 / S-02.** Local disk and LocMem cache are per container. Production guards exist; do not set the `ALLOW_*` flags.
- **S-03.** Huey work can be lost on a hard kill. The 10-minute SIGINT grace is the mitigation this layout uses.
- **S-04.** Connection count grows linearly with workers. Add RDS Proxy or Cloud SQL Auth Proxy pooling before a large replica count.
- **S-05.** One small Redis for queue and cache will evict or block the other workload. Split them.
- **S-11.** The image still defaults to `runserver` and runs as root. The production command override above replaces runserver. A non-root user in the image is still worth doing before a broad rollout.
- **S-13.** Every replica running migrations will collide. Only the migrate task runs them.

Ollama on the same host as the web task (`127.0.0.1:11434`) does not work once web and worker are separate tasks. Point `OLLAMA_LOCAL_HOST` at a stable internal service, or leave local judges off (`OPTIMIZER_JUDGES_PREFER_LOCAL` stays false, which is the code default). Tenant-supplied hosts are restricted to loopback, private LAN, that platform host, and `OLLAMA_LOCAL_ALLOWED_HOSTS`. Link-local and metadata addresses are rejected, and the process environment is not rewritten.
