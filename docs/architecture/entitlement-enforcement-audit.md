# SaaS Entitlement & Capability Enforcement Audit

> [!IMPORTANT]
> **Source of Truth:**
> This document is the technical, code-level source of truth for subscription entitlements, quotas, call-site enforcement gates, and lifecycle controls across HireEdge. For commercial plan descriptions and pricing presentation, refer to [`docs/features/subscription-plans-and-capabilities.md`](../features/subscription-plans-and-capabilities.md).

---

## 1. Purpose & Source of Truth

HireEdge uses a **quota-gated action model** rather than route-level tier packaging. All authenticated users can navigate into the core workspaces (Find Jobs, Career Cockpit, Resume Optimizer, Interview Prep, Tracks). Denial occurs strictly at action execution time when quota checks fail or feature flags are evaluated.

### Core Architectural Taxonomy

| Classification | Definition | Examples | Control Mechanism |
| :--- | :--- | :--- | :--- |
| **Quota Metric** | Consumable numeric counter tracking daily or storage volume | `llm_requests`, `llm_tokens`, `job_searches`, `storage_mb` | Checked via `check_quota()` / `assert_upload_allowed()`; tracked in `UsageCounter` |
| **Boolean Feature Flag** | Binary gate declared on `Plan` determining access to specific capabilities | `api_access`, `byok_required_for_completion` | Checked via `require_api_access()` or direct plan inspection |
| **Commercial Derived Metric** | Marketing packaging presenting daily caps in monthly terms | "5M tokens / month" (30 × 167k daily cap) | Daily quota reset at midnight (`period_date=timezone.localdate()`) |
| **Administrative UX Mode** | User/staff attribute enabling advanced debugging tools; **not** a plan | `is_power_user` (staff advanced debug panels in templates) | Attribute on User model; completely decoupled from the commercial `Power` plan |

---

## 2. Control-Plane Architecture

```
flowchart TD
    subgraph Ingestion & Auth
        Req[Incoming HTTP Request] --> Auth[LoginRequiredMiddleware / SessionOrApiKeyAuth]
        Auth --> View[Django View / Ninja API Router]
    end

    subgraph Entitlement Resolution
        View --> ResolvePlan[subscriptions.get_user_plan]
        ResolvePlan --> SubCheck{Active Subscription?}
        SubCheck -- Yes: active/trialing --> PlanPlan[Return sub.plan]
        SubCheck -- Yes: past_due --> GraceCheck{past_due_since <= 5 days?}
        GraceCheck -- In Grace --> PlanPlan
        GraceCheck -- Grace Expired --> DegradeFree[Return default 'free' plan]
        SubCheck -- No or canceled/unpaid --> DegradeFree
    end

    subgraph Action Call-Site Gates
        View --> GateType{Action Type}
        GateType -- LLM Invoke --> TokenGate[policy.check_token_budget]
        TokenGate --> ReqGate[rate_limits.check_user_llm_rate_limit]
        ReqGate --> LLMCall[llm.gateway.invoke_llm_messages]
        LLMCall --> Finalize[gateway._finalize_usage]
        Finalize --> RecordReq[consume_quota: llm_requests]
        Finalize --> RecordTok[consume_token_budget: llm_tokens]

        GateType -- Job Search --> SearchGate[job_search_core.run_job_search_core]
        SearchGate --> SearchQuota[consume_quota: job_searches]

        GateType -- File Upload --> StorageGate[storage_quota.assert_upload_allowed]
        StorageGate --> MediaCheck[user_storage_bytes + incoming <= limit]

        GateType -- Headless Complete --> FlagGate[require_api_access / byok_check]
    end
```

### Key Enforcement Characteristics
1. **No Route-Level Entitlement Middleware:** HTTP endpoints do not block GET requests based on subscription plans. Users on the Free tier can view the Career Cockpit, Pipeline Board, and Optimizer.
2. **Action-Time Gating:** Entitlement checks are placed directly in domain execution functions (`run_job_search_core`, `gateway._finalize_usage`, `assert_upload_allowed`).
3. **Atomic Quota Engine:** Usage is tracked in durable `UsageCounter` rows keyed to `(owner_id, metric, period_date)`. Quota verification and consumption run through `check_quota(user, metric, cost)` and `consume_quota(user, metric, cost)`.
4. **Staff Bypass:** Staff and superuser accounts bypass all quotas when `settings.SAAS_STAFF_BYPASS_QUOTAS=True`.

---

## 3. Plan Model Field Inventory

Below is the exhaustive inventory of all columns on `resume_app.models.Plan` as of migration `0069`:

| Column Name | Type | Default | Enforcement Status | Notes & Call Sites |
| :--- | :--- | :--- | :--- | :--- |
| `id` | BigAutoField | Primary Key | Enforced | Internal identifier |
| `slug` | SlugField | Unique | Enforced | Commercial slug (`free`, `byok`, `pro`, `power`) |
| `name` | CharField(64) | — | Display Only | User-facing plan label |
| `description` | TextField | `""` | Display Only | Plan marketing description |
| `price_display` | CharField(32) | `"$0 / mo"` | Display Only | Commercial price text on checkout cards |
| `stripe_price_id` | CharField(128) | `""` | Enforced | Bound to Stripe Checkout creation (`create_checkout_session`) |
| `is_default` | BooleanField | `False` | Enforced | Seeded default assigned to newly registered users (`free`) |
| `is_active` | BooleanField | `True` | Enforced | Determines availability in public checkout and admin |
| `sort_order` | PositiveIntegerField | `0` | Enforced | Order on `/billing` and `/pricing` cards |
| `llm_requests_per_day` | PositiveIntegerField | `50` | **Enforced** | Pre-checked in `check_user_llm_rate_limit`; consumed in `gateway._finalize_usage` |
| `llm_tokens_per_day` | PositiveIntegerField | `50000` (free default; was `8300` pre-`0070`) | **Enforced** | Pre-checked in `check_token_budget`; reconciled in `_finalize_usage` |
| `job_searches_per_day` | PositiveIntegerField | `5` | **Enforced** | Consumed in `job_search_core.run_job_search_core` (HTTP 429 on exhaustion) |
| `storage_mb` | PositiveIntegerField | `250` | **Enforced** | Pre-checked in `assert_upload_allowed` across all upload paths |
| `api_access` | BooleanField | `False` | **Enforced** | Gates `CustomerApiKey` generation and bearer REST API authentication |
| `byok_required_for_completion` | BooleanField | `False` | **Enforced** | Blocks platform key usage on `/api/resume/llm/complete` |
| *`apply_runs_per_day`* | *Removed* | *N/A* | **DECOMMISSIONED** | Permanently dropped from database schema via migration `0068` |

---

## 4. Call-Site Enforcement Map

Every quota and feature flag is mapped to its concrete code execution site:

| Capability / Resource | Gating Parameter | Primary Call Site (`file:function`) | Failure Mode / HTTP Code | Resolution Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| **Job Searches** | `Plan.job_searches_per_day` | `resume_app/job_search_core.py:run_job_search_core` | `QuotaExceeded` → HTTP 429 in `jobs_api.py` | Consumes atomic counter `job_searches` |
| **LLM Request Budget** | `Plan.llm_requests_per_day` | `resume_app/llm/gateway.py:_finalize_usage` | `LLMUserRateLimitExceeded` → HTTP 429 in `api.py` | Pre-checks capacity; consumed only on successful invocation |
| **LLM Daily Tokens** | `Plan.llm_tokens_per_day` | `resume_app/llm/policy.py:check_token_budget` | `LLMTokenBudgetExceeded` → HTTP 429 in `api.py` | Pre-checks estimated tokens; consumed in `_finalize_usage` |
| **Resume Upload (Direct)** | `Plan.storage_mb` | `resume_app/views.py:upload_resume` | `QuotaExceeded` → JSON error / HTTP 429 | Gated by `assert_upload_allowed` |
| **Resume Upload (Pipeline Board)** | `Plan.storage_mb` | `resume_app/pipeline_board.py:791` | `QuotaExceeded` → Redirect with error message | Gated by `assert_upload_allowed` |
| **Resume Upload (Career Profiler)** | `Plan.storage_mb` | `resume_app/career_profiler_views.py:97` | `QuotaExceeded` → Redirect with error message | Gated by `assert_upload_allowed` |
| **API Key Provisioning** | `Plan.api_access` | `resume_app/settings_views.py:customer_api_keys_view` | `EntitlementDenied` → 403 Forbidden | Gated by `require_api_access(user)` |
| **Headless Completion** | `Plan.api_access` | `resume_app/api.py:complete_llm` | `403 Forbidden` | Requires active `api_access` flag |
| **Headless Cloud BYOK Rails** | `Plan.byok_required_for_completion` | `resume_app/api.py:complete_llm` | `403 Forbidden` ("BYOK required") | Blocks platform keys if flag is True |
| **Scheduled Job Tasks** | `Plan.job_searches_per_day` | `resume_app/tasks.py:run_job_search_task` | `run.status = STATUS_FAILED` | Fail-closed in Huey worker |

---

## 5. Token & Request Control Deep-Dive

### Lifecycle of an LLM Request
1. **Pre-Check Phase:**
   - In `invoke_llm_messages`, token usage is estimated from message text via `estimate_tokens_from_messages(messages)`.
   - `check_token_budget(user, estimated_tokens, provider, is_local)` validates that the user's daily budget in `UsageCounter(owner, 'llm_tokens')` has sufficient headroom.
   - `check_user_llm_rate_limit(user, is_local)` validates that the user has at least 1 request remaining under `llm_requests_per_day`.
2. **Candidate Dispatch Phase:**
   - Provider slots are acquired via `try_acquire_llm_slot(provider, model)`.
   - If a platform rate limit or timeout occurs, the gateway fails over to subsequent candidates **without** charging the user's request quota.
3. **Finalization Phase:**
   - On successful invocation, `_finalize_usage()` is called.
   - Reconciles estimated tokens with provider-reported actual token counts (`raw.usage_metadata`).
   - Atomically records exact tokens via `consume_token_budget()`.
   - Atomically increments daily request count via `record_llm_request()`.

### BYOK and Local Execution
- **Local Providers (Ollama, mock, unsloth):** Skip user request quotas, token consumption, and platform Redis rate limits (`is_local=True`).
- **User BYOK Keys:** BYOK calls consume the candidate user's daily request/token budget if on standard plans, but skip platform global concurrency buckets. On the `byok` plan (`llm_tokens_per_day=0`), direct token consumption is unmetered.

---

## 6. Feature-Flag & Authentication Architecture

### Programmatic API Access vs Browser REST
- **Browser Session REST Endpoints:** Endpoints serving the frontend web app (`/api/resume/optimize`, `/api/resume/jobs/search`, `/api/resume/jobs/recommendations`) authenticate via `SessionOrApiKeyAuth`. Any authenticated user session is permitted regardless of plan tier. Quota consumption occurs at action time.
- **Developer API Key Access:** Only users on plans with `api_access=True` (`byok`, `pro`, `power`) can generate `re_live_` API keys and invoke `/api/resume/llm/complete` or bearer-authenticated APIs.

---

## 7. UI & Billing Lifecycle

### Stripe Webhook & Payment Grace Engine
1. **`invoice.payment_failed`:** Sets subscription status to `past_due` and initializes `past_due_since = timezone.now()` (handled in `resume_app.subscriptions.billing.apply_stripe_event`).
2. **Grace Period Calculation:** `get_user_plan()` evaluates:
   ```python
   ref_time = sub.past_due_since or sub.updated_at
   if ref_time and ref_time < cutoff: # 5 days ago
       return fallback_free_plan
   return sub.plan
   ```
3. **Data Backfill (Migration `0069`):** Pre-existing `past_due` rows with `past_due_since=NULL` were backfilled to `past_due_since = updated_at`, ensuring no account maintains indefinite grace through `updated_at` touches.
4. **Admin Bulk Action Integrity:** `SubscriptionAdmin.mark_active` and `mark_canceled` explicitly clear `past_due_since=None`, preventing stale grace clocks if accounts re-enter past-due status later.

---

## 8. Audit Remediation Scorecard

| Gap ID | Identified Finding | Initial Status | Resolution in Codebase |
| :--- | :--- | :--- | :--- |
| **GAP-01** | Dead `apply_runs_per_day` column in `Plan` | Obsolete in DB | Dropped column in migration `0068`; deleted `METRIC_APPLY_RUNS` in code. |
| **GAP-02** | Documentation implied Free tier blocked from session REST | Discrepancy | Clarified `SessionOrApiKeyAuth` opens UI endpoints; `api_access` restricts keys. |
| **GAP-03** | `pipeline_board` resume upload bypassed storage quota | Bypassed | Added `assert_upload_allowed` in `pipeline_board.py:791`. |
| **GAP-04** | Missing `past_due_since` data migration backfill | Incomplete | Added migration `0069_backfill_past_due_since.py`; backfilled legacy rows. |
| **GAP-05** | Admin bulk status updates bypassed `save()`, leaving stale grace clock | Stale clock | Updated `mark_active` and `mark_canceled` in `admin.py` to clear `past_due_since`. |
| **GAP-06** | LLM request quota charged inside candidate retry loop | Double count | Moved `record_llm_request` to `_finalize_usage`, charging strictly upon success. |
| **GAP-07** | Inconsistent quota HTTP status codes across APIs | Mixed 400/500 | Normalized all quota exhaustion to standard HTTP 429 Too Many Requests. |

---

## 9. Follow-Up Hardening Roadmap (Non-Blockers)

1. **Storage Concurrency Locking (TOCTOU):** Introduce user-level advisory locks for multi-part file uploads if upload concurrency increases.
2. **Extract Directory Pruning:** Implement scheduled cleanup for legacy `pipeline_llm_extract` run directories older than 30 days.
3. **Huey Scheduled Quota Telemetry:** Log `QuotaExceeded` in `run_job_search_task` at `INFO` level to prevent DLQ exception noise for degraded accounts.
