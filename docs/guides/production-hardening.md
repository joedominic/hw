# Production Hardening & Fail-Closed Guardrails

This document specifies the launch-readiness invariants, fail-closed production deployment gates, zero-data telemetry accuracy standards, and workflow monotonicity rules enforced in HireEdge.

---

## 1. Fail-Closed Production Startup Invariants

To eliminate silent misconfiguration risks during live deployments, `django_project/core/settings.py` enforces strict fail-closed checks when `DEBUG=False` (and outside test execution):

| Invariant | Production Check | Remediation | Explicit Override Flag |
|---|---|---|---|
| **Secret Key** | Cannot be empty or match known insecure development keys (`django-insecure-*`). | Set a cryptographically random `SECRET_KEY` in `.env` / vault. | N/A |
| **Allowed Hosts** | `ALLOWED_HOSTS` cannot be empty. | Configure production domain(s) (e.g. `hireedge.com,app.hireedge.com`). | N/A |
| **Database Engine** | Rejects `django.db.backends.sqlite3` due to concurrency and database-locked issues with Huey workers. | Configure MySQL / MariaDB via `MYSQL_DATABASE`, `MYSQL_USER`, etc. | `ALLOW_SQLITE_IN_PRODUCTION=1` |
| **Transactional Email** | Rejects `django.core.mail.backends.console.EmailBackend`. | Configure SMTP or production transactional email gateway. | `ALLOW_CONSOLE_EMAIL_IN_PRODUCTION=1` |
| **Stripe Webhook Secret** | `STRIPE_WEBHOOK_SECRET` must be set whenever `STRIPE_SECRET_KEY` is present. | Set Stripe signing secret from Stripe Dashboard. | `ALLOW_EMPTY_STRIPE_WEBHOOK_SECRET=1` |

When any required production condition is violated, the Django process terminates immediately on boot with a descriptive `django.core.exceptions.ImproperlyConfigured` exception.

---

## 2. Zero-Data Dashboard Contract (Truth in Analytics)

The Career Performance Dashboard (`/performance/`, computed by `resume_app.dashboard_stats.get_performance_dashboard_stats`) strictly enforces truth-in-advertising. Hardcoded fallback statistics and mock defaults are forbidden:

1. **New User Baseline:** A newly created account with no activity renders:
   - **Active Streak:** `0` days.
   - **Scanned Market Listings:** `0` (or the actual count of ingested listings in `JobListing`).
   - **Pipeline Opportunities:** `0`.
   - **Submitted Applications:** `0`.
   - **Applications Ready Queue:** `0`.
   - **Opportunity Pipeline Valuation:** `$0` (or `"[N] Roles"` if target compensation is unconfigured; never invented millions).
   - **Executive Time Saved:** `0 Hours`.
   - **Preserved Capital:** `$0`.
   - **Verified Turnaround:** `"Awaiting first resume"`.
   - **Factual Integrity:** `"—"` / `"No tailored resumes yet"`.
2. **Quota Attribution:** Quotas and consumed credits are derived directly from the user's active `Plan` and daily `UsageCounter` entries via `resume_app.subscriptions.entitlements.plan_limit`.
3. **Template Neutrality:** All templates render authentic data or clean zero/empty indicators without fabricated fallbacks.

---

## 3. Workflow Monotonicity & Unsave Safety

To preserve candidate audit records and prevent accidental data loss:

1. **Protected Unsave:**
   - Calling `POST /api/resume/jobs/{id}/unsave` or `remove_pipeline_entry_on_unsave` removes the `SAVED` action and only soft-deletes `PipelineEntry` rows in `VETTING` (Review) or default stages.
   - Rows in `APPLYING` (tailored/staged) or `DONE` (applied) stages are **preserved**. Application history, employer responses, and interview event notes are never deleted upon an unsave action.
2. **Monotonic Saves:**
   - Re-saving or restoring a job listing via `ensure_pipeline_entry_on_manual_save` will not regress an in-flight or completed application (`APPLYING` or `DONE`) back to `VETTING`.
3. **Idempotent Skill Radar:**
   - Re-analysis or database mutation on `cockpit_skill_radar_api` requires a CSRF-protected `POST` request.
   - `GET` requests provide idempotent, read-only inspection of previously saved or cached diagnostics without executing background writes.

---

## 4. Privacy & Telemetry Production Redaction

1. **Default Redaction:** In production (`DEBUG=False`), `LANGFUSE_REDACT_CONTENT` defaults to `True`.
2. **Zero-Content Export:** LiteLLM applies `mask_input=True` and `mask_output=True`, ensuring no raw resume bullets, project history, or LLM reasoning are exported to Langfuse Cloud. Only operational telemetry (token counts, latency, model names) is transmitted.
3. **Explicit Opt-In:** Full prompt export requires explicitly passing `LANGFUSE_EXPORT_PROMPT_CONTENT=True`.

---

## 5. Toggleable OpenTelemetry & Observability

HireEdge incorporates standardized, vendor-neutral OpenTelemetry (OTel) instrumentation for application and database observability:

1. **Strictly Toggleable:**
   - Inactive by default (`OTEL_ENABLED=False`). Zero runtime overhead and no outbound network calls occur when disabled.
2. **Vendor-Agnostic OTLP:**
   - When enabled, traces are exported via standard OTLP (gRPC / HTTP) compatible with SigNoz, Grafana Tempo, Datadog, Honeycomb, or AWS X-Ray.
3. **Correlation with Langfuse:**
   - Trace IDs from the active OTel span context are injected into downstream LLM call metadata (`llm_metadata["trace_id"]`), enabling seamless correlation between web/worker traces and Langfuse LLM generation traces.
4. **Structured Logging:**
   - Django logging formats output with standardized ISO timestamps, log level, module name, and message format.

---

## 6. Automated Verification

These rules are continuously verified by [`resume_app.test_zero_data_dashboard`](../../django_project/resume_app/test_zero_data_dashboard.py):

```powershell
python manage.py test resume_app.test_zero_data_dashboard
```
