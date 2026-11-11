# Statelessness & Horizontal Scaling Audit

**Date:** 2026-11-08
**Scope:** Can the web tier and Huey worker tier run as stateless containers, and what prevents scaling from 1 to 100 pods?
**Verdict:** **Conditionally stateless, not yet safe for 100 pods.** All durable application state is designed to live in MySQL, Redis, and S3. However, several defaults silently fall back to per-container state, several distributed primitives are not crash-safe, and shared infrastructure (MySQL connections, a single 512 MB Redis) caps scale long before 100 pods.

> This audit corrects claims in [Container Scaling & Deployment](../guides/container-scaling-and-deployment.md). That guide describes the *intended* posture; this document lists where the code diverges from it.

---

## 1. What happens if you deploy today as stateless containers

| Scenario | Outcome |
| :--- | :--- |
| `AWS_STORAGE_BUCKET_NAME` unset | Uploaded resumes and pipeline extracts are written to each pod's ephemeral disk. Other pods (and Huey workers) cannot read them; everything is lost on restart. **No startup guard prevents this.** |
| `DEBUG` unset in the manifest | `DEBUG` defaults to **True** → Django cache falls back to per-process `LocMemCache` → leader locks, throttles, circuit breakers, job-search locks, and task progress keys become per-process. Every worker pod runs every cron job. |
| Image deployed with default `CMD` | Runs `manage.py runserver` (dev server) as **root**. |
| Pod scale-in / rolling deploy kills a Huey worker | In-flight tasks are lost (Huey has no ack/visibility timeout). Optimization runs stay `running` forever and block re-runs for that pipeline entry. |
| Scale to ~25+ pods | MySQL `max_connections=151` is exhausted. |
| Embedding cache grows | Redis (512 MB, `noeviction`) refuses writes → Huey cannot enqueue and the LLM limiter fails closed → platform-wide outage. |

### What *is* correctly externalized

- Sessions: database-backed (default `SESSION_ENGINE`), no sticky sessions needed.
- Static assets: baked into the image via `collectstatic` + WhiteNoise.
- Media in S3 mode: `media_access.serve_media_view` issues presigned redirects; pipeline extracts route through `default_storage`.
- Periodic tasks: guarded by `acquire_periodic_leader_lock` (Redis `cache.add`).
- LLM rate limits and per-user concurrency: Redis.
- Stripe webhooks: idempotency table `StripeWebhookEvent` (with a race; see S-10).
- Logging: stdout only.

### Process-local state inventory

| Location | State | Cross-pod impact |
| :--- | :--- | :--- |
| `resume_app/embeddings.py` `_ENGINE` | Loaded SentenceTransformer / ONNX model | Not shared state, but costs memory and cold-start time per process (S-14, S-15). |
| `resume_app/agents.py` `_NODE_LLM_DEBUG_QUEUES` | Per-run debug payload FIFO | Safe while a LangGraph run stays in one process; entries leak if a run crashes before `clear_node_llm_debug`. |
| `resume_app/sourcing/circuit_breaker.py` `_circuit_breakers` | Object registry only | Safe because state lives in the cache, but only when the cache is Redis (S-02). |
| `resume_app/llm/rate_limit.py` `_redis_client` | Connection pool | Safe. |
| `CACHES` → `LocMemCache` fallback | **All cache-backed coordination** | Unsafe (S-02). |
| `MEDIA_ROOT` → `FileSystemStorage` fallback | **All uploaded and generated files** | Unsafe (S-01). |

---

## 2. Findings

Severity: **Critical** = data loss or outage at any replica count > 1. **High** = breaks before 100 pods or under routine pod churn. **Medium** = degraded correctness/efficiency at scale. **Low** = hygiene.

### Critical

#### S-01 — Local-disk media is the silent default in production
- **Where:** `core/settings.py` `STORAGES["default"]` falls back to `FileSystemStorage` when `AWS_STORAGE_BUCKET_NAME` is empty; the production invariant block (bottom of `settings.py`) has no check for it.
- **Impact:** Resume uploads on web pod A cannot be parsed by Huey pod B; `pipeline_llm_extract/*` progress written by a worker is invisible to the web pod polling `read_run_status`; all files vanish on pod restart.
- **Fix:** Fail closed in production unless explicitly opted in:
  ```python
  if not AWS_STORAGE_BUCKET_NAME and not env.bool("ALLOW_LOCAL_MEDIA_IN_PRODUCTION", default=False):
      raise ImproperlyConfigured(
          "Production storage failure: set AWS_STORAGE_BUCKET_NAME for multi-replica deployments "
          "or ALLOW_LOCAL_MEDIA_IN_PRODUCTION=1 for single-node Compose with a shared volume."
      )
  ```

#### S-02 — Cache silently degrades to per-process `LocMemCache`
- **Where:** `core/settings.py` `_USE_REDIS_CACHE` is False when `DEBUG=True`, `HUEY_IMMEDIATE=1`, or `DJANGO_CACHE_USE_REDIS=0`. `DEBUG` itself defaults to **True** (`environ.Env(DEBUG=(bool, True))`).
- **Breaks:** `acquire_periodic_leader_lock` (every pod runs every cron), job-search and vetting per-user locks (`tasks.py`), `job_task_pending:*` / progress keys written by workers and read by web, `AbuseThrottleMiddleware`, `SourceCircuitBreaker`, embedding cache.
- **Masking:** `/health/ready` writes and reads `_readiness_check` through the same cache, so it reports `cache: ok` on LocMem.
- **Fix:** Introduce an explicit `APP_ENV` profile (`production` baked into the runtime image, `development` when unset) and derive `DEBUG` from it. Raise `ImproperlyConfigured` when `APP_ENV=production` and the cache is LocMem; have the readiness probe report the cache backend. Local development keeps LocMem as a valid fallback (see the remediation plan, L-01).

#### S-03 — Huey tasks are lost on pod termination; no recovery for optimization runs
- **Where:** Huey dequeues with a destructive pop from Redis (no ack). Huey 2.x treats `SIGTERM` (what Kubernetes and Docker send) as **immediate** shutdown; only `SIGINT` drains gracefully.
- **Impact:** Every scale-in and rolling deploy aborts running LLM optimizations, pipeline extracts, and job searches. There is a reaper for `JobSearchTaskRun` (`mark_stale_job_search_runs_failed`), but **none for `OptimizedResume`**. A stuck `STATUS_RUNNING`/`STATUS_QUEUED` row makes the pipeline optimizer return "Optimization already queued or running" (`tasks.py` ~L585) indefinitely. Pipeline extract `progress.json` stays `phase: running`.
- **Fix:**
  1. Send `SIGINT` on shutdown (`STOPSIGNAL SIGINT` in the Dockerfile, or a `preStop` hook) and set `terminationGracePeriodSeconds` ≥ the longest task (`LLM_INVOKE_TIMEOUT_SECONDS=480`).
  2. Add a periodic reaper for `OptimizedResume` and pipeline-extract runs, keyed on `updated_at`/heartbeat.
  3. Make long tasks resumable or idempotent so they can be re-enqueued safely.

#### S-04 — MySQL connection budget is exhausted well before 100 pods
- **Where:** `CONN_MAX_AGE=60`, Gunicorn `--workers 2` (sync), Huey `workers: 2` threads, plus extra threads that touch the ORM (`run_with_invoke_timeout` executor threads, Levels.fyi enrichment pool `LEVELS_FYI_ENRICH_MAX_WORKERS=8`).
- **Math:** 100 web pods × 2 + 100 worker pods × 2 = 400 persistent connections at minimum, versus MariaDB's default `max_connections=151`. ORM access from short-lived threads opens connections that are never closed by Django's request lifecycle.
- **Fix:** Put ProxySQL / RDS Proxy in front of MySQL; lower `CONN_MAX_AGE` or set it to `0` for worker threads; call `django.db.close_old_connections()` at the end of any thread that touches the ORM; raise `max_connections` deliberately.

#### S-05 — Single 512 MB `noeviction` Redis shared by queue, cache, limiter, and embeddings
- **Where:** `ops/redis/redis-jobapp.conf` (`maxmemory 512mb`, `maxmemory-policy noeviction`). Huey (DB 0), LLM limiter (DB 0 by default), and Django cache (DB 1) share one instance. `embeddings._encode_texts_cached` stores 384-float vectors (about 4 KB pickled) for **7 days** (`EMBEDDING_CACHE_TTL`).
- **Impact:** At roughly 100k cached embeddings, Redis rejects all writes (`OOM command not allowed`). Huey enqueues fail, `user_llm_concurrency` fails closed (`LLM_RATE_LIMIT_FAIL_OPEN=False` in prod), and readiness flips to 503 on **every pod at once**.
- **Fix:** Split into two Redis deployments: a queue/limiter instance with `noeviction` and a cache instance with `allkeys-lru`. Point `DJANGO_CACHE_URL` at the cache instance. Size for scale and run HA (Sentinel / managed Redis).

### High

#### S-06 — Per-user LLM concurrency slots leak on pod or worker kill
- **Where:** `llm/policy.py` `user_llm_concurrency`: `INCR` followed by `EXPIRE 600` on **every** acquire; release is `DECR` in `finally`.
- **Impact:** If Gunicorn's 120 s timeout kills a worker mid-call, or a pod is `SIGKILL`ed, the `finally` never runs. Because each new acquire resets the TTL to 600 s, an active user's leaked count **never expires**. With `LLM_USER_MAX_CONCURRENT=2`, two leaks lock the user out of all AI features.
- **Fix:** Use a Redis sorted set of lease IDs scored by expiry (`ZADD key <now+ttl> <uuid>`, `ZREMRANGEBYSCORE key -inf <now>`, `ZCARD`) inside a Lua script; release with `ZREM`.

#### S-07 — `run_with_invoke_timeout` does not enforce its timeout
- **Where:** `llm/policy.py` L331–343, used by `llm/gateway.py` L1093 and L1233.
- **Impact:** `with ThreadPoolExecutor(...)` calls `shutdown(wait=True)` on exit, so after `fut.result(timeout=...)` raises, the `with` block **blocks until the LLM call finishes**. The caller (a Gunicorn worker or Huey thread) is held for the full provider latency, which defeats capacity planning.
- **Fix:** Create the executor outside a `with` block and call `pool.shutdown(wait=False, cancel_futures=True)` on timeout. Better, pass provider-native HTTP timeouts to the LangChain client so the socket is actually closed.

#### S-08 — Synchronous LLM and embedding work in the web request path
- **Where:** `jobs_api.py` ~L638 (loops `run_matching` over every `job_listing_id` in one request), `views.py` L1048 and `settings_views.py` L492 (direct `llm.invoke`), embedding calls in `views.py` L2844 and `career_profiler_views.py` L336/L350.
- **Impact:** With 2 sync Gunicorn workers per pod, two slow LLM requests make the pod unresponsive, including to `/health/*` probes. Gunicorn `--timeout 120` is shorter than `LLM_INVOKE_TIMEOUT_SECONDS=480`, so workers are killed mid-call (which feeds S-06). Request-driven autoscaling cannot fix latency that is bound by an upstream LLM.
- **Fix:** Move multi-job matching to Huey with polling, as the optimizer already does. Use `gthread` workers (`--worker-class gthread --threads 8`) for I/O-bound views. Cap `job_listing_ids` per request.

#### S-09 — Periodic leader lock is not tied to the schedule tick
- **Where:** `tasks.py` `acquire_periodic_leader_lock` uses key `periodic_leader_lock:{task_name}` with TTLs of 45–300 s.
- **Impact:** Each Huey consumer's scheduler enqueues its own copy of every periodic task, so 100 worker pods produce 100 queue entries per tick. Copies wait behind multi-minute LLM tasks (2 threads per pod). Any copy dequeued **after the TTL expires** wins a fresh lock and runs again: `pipeline_manager` (TTL 120 s), `cleanup_manager` and `purge_generated_resumes_periodic` (TTL 300 s) then fan out duplicate per-user work.
- **Fix:** Key the lock on the scheduled slot, e.g. `periodic_leader_lock:{name}:{now.replace(second=0, microsecond=0).isoformat()}`, with TTL ≥ the cron period. Alternatively, run periodic scheduling in a single dedicated `huey` pod (`-n`/`--no-periodic` on the others).

#### S-10 — Stripe webhook idempotency is check-then-act
- **Where:** `subscriptions/billing.py` `apply_stripe_event`: `filter(...).exists()` → apply side effects → `create(event_id=...)`, not inside a transaction.
- **Impact:** Stripe retries can land on two pods concurrently. Both apply the event, and the second `create` raises `IntegrityError` → 500 → Stripe retries again. Effects are mostly idempotent today, but this pattern breaks any non-idempotent handler added later.
- **Fix:** Wrap the handler in `transaction.atomic()` and insert the `StripeWebhookEvent` row **first** (catch `IntegrityError` → return `False`), then apply effects.

#### S-11 — Container image is not production-shaped
- **Where:** `Dockerfile`.
- **Issues:** Single-stage build; runs as **root**; default `CMD` is `runserver`; PyTorch + sentence-transformers make the image several GB, so pulls on new nodes take minutes during scale-out bursts; no `STOPSIGNAL`; no `HEALTHCHECK`.
- **Fix:** Multi-stage build, non-root user, `CMD` = Gunicorn, `STOPSIGNAL SIGINT` for the worker target, and separate slim `web` / heavy `worker` targets if embeddings can move to the worker tier.

#### S-12 — Health probes break behind TLS and couple all pods to shared dependencies
- **Where:** `views.health_ready_view`, `SECURE_SSL_REDIRECT=True` (prod default), `ALLOWED_HOSTS`, `docker-compose.prod.yml` healthcheck.
- **Issues:**
  - Probes from kubelet or the compose healthcheck use plain HTTP with `Host: <pod-ip>` or `127.0.0.1`. `SecurityMiddleware` either 400s (`DisallowedHost`) or 301s to HTTPS. Kubelet counts 3xx as success, so the DB/Redis check **never runs**. The compose healthcheck follows the redirect to `https://127.0.0.1:8000` and fails.
  - Readiness checks shared MySQL/Redis, so a Redis blip marks **every** pod unready at once and the load balancer has zero backends.
  - With sync workers busy on LLM calls (S-08), liveness probes time out and restart pods mid-request.
- **Fix:** Add `SECURE_REDIRECT_EXEMPT = [r"^health/"]`, exempt health paths from host validation (or set `ALLOWED_HOSTS` to include the probe host), keep liveness dependency-free, and do not fail readiness on transient shared-dependency errors.

#### S-13 — Startup migrations race under Compose scaling and are not rollout-safe
- **Where:** `docker-compose.prod.yml` sets `RUN_MIGRATIONS: "1"` on `web`; `docker compose up --scale web=N` runs N concurrent `migrate`.
- **Impact:** Concurrent DDL can fail or deadlock. During rolling updates, old pods run against the new schema; non-additive migrations (column drops/renames) break them.
- **Fix:** Run migrations in a one-shot Job or CI step only, and adopt expand/contract migrations.

### Medium

#### S-14 — Embedding model loaded per process and downloaded per pod
- **Where:** `embeddings.py` `EmbeddingEngine._init_model` calls `hf_hub_download` / `SentenceTransformer(_MODEL_NAME)` at first use. `HF_HOME=/root/.cache/huggingface` is a Compose volume, not baked into the image. `tiktoken` similarly downloads encodings on first use.
- **Impact:** Every new pod downloads from Hugging Face on its **first user request** (cold-start latency, HF rate limits, outage if HF is unreachable). Each Gunicorn worker and Huey process holds its own copy (about 100 MB ONNX / 400+ MB torch).
- **Fix:** Download the model and tiktoken encodings during `docker build` (set `HF_HUB_OFFLINE=1`, `TIKTOKEN_CACHE_DIR`). Warm the engine in a startup hook before the pod reports ready. Consider Gunicorn `--preload` for copy-on-write sharing.

#### S-15 — Scale-out cold starts are expensive
Large image pull (S-11), model download (S-14), and lazy model load mean that new pods take tens of seconds to minutes to serve fast requests. Autoscaling 1 → 100 under a spike will lag. Use pre-warmed minimum replicas and readiness gated on warm-up.

#### S-16 — S3 pipeline-extract writes are non-atomic and O(n²)
- **Where:** `pipeline_llm_skill_extract.py` `append_skills_batch`, `write_progress`, `_write_consolidated_skills`, `_finalize_meta`, `write_initial_run_files`.
- **Impact:** Each append downloads the whole NDJSON object, deletes it, and re-uploads it, so large runs make O(n²) traffic. The `delete` → `save` gap means a concurrent `read_run_status` poll from another pod gets "not found" and the UI reports the run as missing. Concurrent writers can lose lines.
- **Fix:** Store progress and batch rows in the database (a `PipelineExtractBatch` model) or write one object per batch (`skills/{index}.json`). Use a single overwrite `save` (with `AWS_S3_FILE_OVERWRITE=True`) instead of delete-then-save.

#### S-17 — Abuse throttle is racy and spoofable
- **Where:** `abuse.py` `is_rate_limited` (get → modify → set) and `_client_ip` (trusts the **first** `X-Forwarded-For` entry).
- **Impact:** Concurrent requests across pods undercount. Any client can send an arbitrary `X-Forwarded-For` and get a fresh bucket per request, bypassing login brute-force protection.
- **Fix:** Use atomic `cache.incr` with `cache.add` for window init (or a Redis Lua script), and derive client IP from the right-most trusted proxy hop (configure the number of trusted proxies).

#### S-18 — Circuit breaker read-modify-write and half-open stampede
- **Where:** `sourcing/circuit_breaker.py`.
- **Impact:** Concurrent failures across pods overwrite each other's counts (the threshold trips late). When cooldown expires, every pod's requests are admitted as "probes" simultaneously, so the half-open state does not protect the upstream.
- **Fix:** Atomic `INCR` for failures and a `cache.add`-guarded single probe token for half-open.

#### S-19 — Hard throughput ceilings that do not scale with pods
- `MAX_DUE_JOB_SEARCH_TASKS_PER_TICK = 50` per minute (`tasks.py`): scheduled searches are capped at about 72k/day regardless of worker count, and backlog grows silently.
- Lock-holding fan-out loops (`pipeline_manager`, `cleanup_manager`) enumerate all active users in one task. At large user counts this exceeds the lock TTL (compounds S-09).
- Huey `workers: 2` is hard-coded in `settings.HUEY["consumer"]`; make it env-configurable.

#### S-20 — Stale per-user locks after a worker crash
- **Where:** `run_job_search_task` / vetting tasks take `cache.add(lock_key, 1, 3600)` and release it in `finally`.
- **Impact:** A killed pod (S-03) leaves the user unable to run job searches for up to 1 hour. `mark_stale_job_search_runs_failed` clears `job_task_pending:*` but not the per-user lock.
- **Fix:** Shorter lock TTL with heartbeat renewal, and clear the lock in the stale-run reaper.

#### S-21 — Database-backed sessions put a DB read on every request
- At 100 pods this is the dominant query volume, and large values (e.g. `JOB_LLM_MATCH_SESSION_KEY` payloads) are rewritten per request. Consider `SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"` once Redis is split (S-05), and schedule `clearsessions`.

#### S-22 — Configuration must be identical across pods
- `FERNET_KEYS` empty → keys derive from `SECRET_KEY`. A pod with a different `SECRET_KEY` cannot decrypt stored BYOK API keys or validate sessions. `HUEY_NAME` must match between web and worker pods. Source these from one Secret/ConfigMap and validate at startup.

### Low

#### S-23 — Redis TLS skips certificate verification
`settings.py` sets `ssl_cert_reqs = CERT_NONE` for Huey, and `rate_limit._get_redis` sets `ssl_cert_reqs=None`. Encrypted, but MITM-able on a shared network.

#### S-24 — `dbbackup` writes to container-local `BASE_DIR/backups`
Backups taken inside an ephemeral container are lost. Run as a Job with a mounted volume or upload to object storage.

#### S-25 — Telemetry flush on shutdown
Langfuse and OTel batch exporters may drop buffered spans when pods are killed without a graceful shutdown (S-03). Flush in a shutdown hook.

#### S-26 — Legacy browser-apply knobs default to headed mode
`APPLY_BROWSER_HEADLESS` defaults to `False`. Any surviving Playwright path crashes in a display-less container. Default to `True`, or delete the knobs if the feature is fully removed.

---

## 3. Recommended remediation order

The detailed, complexity-categorized plan lives in [Statelessness Remediation Plan](statelessness-remediation-plan.md).

1. **Fail-closed config (S-01, S-02, S-22):** startup invariants for S3, Redis cache, and `DEBUG=False` default. Small change, eliminates the silent-state failure modes.
2. **Infrastructure split (S-04, S-05):** connection pooler for MySQL; separate queue and cache Redis with HA.
3. **Crash safety (S-03, S-06, S-07, S-20):** `SIGINT` + grace period, `OptimizedResume` reaper, lease-based concurrency, real invoke timeouts.
4. **Web tier (S-08, S-11, S-12, S-14):** move LLM calls off the request path, production image, probe fixes, baked model.
5. **Coordination correctness (S-09, S-10, S-16–S-19):** tick-keyed leader locks, atomic Stripe idempotency, S3 write model, atomic throttles and breakers.

## 4. Minimum production environment for multi-replica deployments

```ini
DEBUG=False
DJANGO_CACHE_USE_REDIS=1
DJANGO_CACHE_URL=redis://:<password>@redis-cache.internal:6379/1   # separate allkeys-lru instance
HUEY_REDIS_HOST=redis-queue.internal                              # noeviction instance
HUEY_NAME=hireedge-prod                                           # identical on web + worker
AWS_STORAGE_BUCKET_NAME=hireedge-media-prod
RUN_MIGRATIONS=0                                                  # migrations run as a Job
FERNET_KEYS=<shared-key>                                          # do not derive from SECRET_KEY
LLM_RATE_LIMIT_FAIL_OPEN=False
```

## 5. Verification checklist

- [ ] `python manage.py check --deploy` passes with production env.
- [ ] `curl -H "Host: <pod-ip>" http://<pod-ip>:8000/health/ready` returns 200 JSON (not 301/400) with `cache: ok` against Redis.
- [ ] Kill a Huey pod mid-optimization: the run is reaped to `failed` and can be re-run.
- [ ] Two web pods: upload a resume on pod A, download it via pod B.
- [ ] Load test with N pods: `SHOW STATUS LIKE 'Threads_connected'` stays under `max_connections`; Redis `used_memory` stays under `maxmemory`.
