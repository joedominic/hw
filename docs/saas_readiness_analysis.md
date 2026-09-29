# HireEdge — SaaS Launch Readiness Analysis

> Deep analysis across functionality and implementation. Generated Aug 30, 2026.

---

## Executive Verdict

**HireEdge is a technically sophisticated, feature-complete internal tool that is 60–70% of the way to a safe public SaaS launch.** The core product loop — job discovery → resume tailoring → pipeline & prep — is production-quality. The gaps are primarily in multi-tenant scalability, legal/compliance polish, and the operational hardening required to serve untrusted strangers at scale.

---

## 1. Authentication & Identity

### What Exists ✅
- Django session-based login with `LoginRequiredMiddleware` enforcing auth globally
- Sign-up flow with optional SIGNUP_ENABLED flag (invite-only mode)
- Email verification system with `REQUIRE_EMAIL_VERIFICATION` setting, `EmailVerification` model, resend flow
- Password reset (full flow: request → email link → confirm → complete via `auth/` templates)
- Password strength validators (similarity, minimum length, common, numeric)
- `django-hijack` staff impersonation with `ImpersonationAuditLog` model
- `CustomerApiKey` model with hashed keys + HMAC prefix lookup for API access
- Unique email enforcement at model level (`0025_user_email_unique`)

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No 2FA / MFA** — no TOTP, SMS, or backup codes | 🔴 High | 2–3 days (`django-otp`) |
| **Email verification not enforced** — `REQUIRE_EMAIL_VERIFICATION=False` by default; users can fully use the product with unverified emails | 🔴 High | 1 day to flip + UX |
| **No social OAuth** — no Google/GitHub login (expected by modern SaaS users) | 🟡 Medium | 2–3 days (`django-allauth`) |
| **No username change flow** — usernames are immutable after registration | 🟡 Medium | 0.5 days |
| **Session timeout not configured** — `SESSION_COOKIE_AGE` not set (Django default: 2 weeks) | 🟡 Medium | 1 hour |
| **No account lockout after failed logins** beyond the `AbuseThrottleMiddleware` at auth paths | 🟡 Medium | 1 day |

---

## 2. Multi-Tenancy & Data Isolation

### What Exists ✅
- `OwnedManager` pattern: `.for_user(user)` and `.get_owned_or_404()` on all owned models
- User-scoped tenancy: every model with user data has `owner = ForeignKey(User)`
- `get_or_404` helpers prevent cross-user reads in views
- `SAAS_STAFF_BYPASS_QUOTAS` flag prevents staff-specific quota exploitation
- 40+ models correctly scoped

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No Organization / Team entity** — 1 user = 1 tenant, no shared workspaces | 🟡 Medium (for B2B) | 3–5 weeks |
| **`JobListing` is a global table** — all users share the same job rows; no private job boards | 🟡 Medium | 2–3 days refactor |
| **`SystemPromptProfile` is global** — cannot have per-org prompt templates | 🟡 Medium | 1 day |
| **No DB-level Row Level Security** — isolation is app-layer only; a developer mistake exposes all user data | 🔴 High (trust boundary) | Architecture change |
| **Global Redis locks** — `JOB_SEARCH_TASK_LOCK_KEY` is not tenant-scoped; one user's search blocks others | 🔴 High | 1 day |
| **Shared IP for scraping** — all users' job searches come from one host IP; one account getting blocked affects all | 🔴 High | Proxy rotation service |

---

## 3. Billing & Monetization

### What Exists ✅
- **Stripe integration**: Checkout Sessions, Billing Portal, Subscription lifecycle webhooks
- **Webhook idempotency** via `StripeWebhookEvent.event_id` dedup check
- **Subscription status tracking**: `TRIALING`, `ACTIVE`, `PAST_DUE`, `CANCELED`, `UNPAID`
- **Dunning handling**: `invoice.payment_failed` → `PAST_DUE`; `invoice.paid` auto-recovers to `ACTIVE`
- **Plan model** with per-plan quotas: `llm_tokens_per_day`, `llm_requests_per_day`, `job_searches_per_day`, storage, API access, `apply_runs_per_day`, price display
- **Free / Pro / Unlimited plan slugs** with quota enforcement via `SAAS_ENFORCE_QUOTAS`
- **Plan-level token budgets** by slug (`LLM_DAILY_TOKEN_LIMIT_BY_PLAN`)
- **Storage quota** tracking (`storage_quota.py`)
- **Staff admin billing** page (`admin_billing.html`)

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No free trial period** — there is `TRIALING` status and Stripe supports trials, but no UI/flow to start a trial | 🟡 Medium | 1 day |
| **No proration UI** — mid-cycle plan changes aren't surfaced to users | 🟡 Medium | 0.5 days |
| **`stripe_price_id` must be manually configured in admin** — no automated Plan sync from Stripe Products API | 🟡 Medium | 0.5 days |
| **No failed payment email** — `PAST_DUE` status updates but no transactional email to user to update payment | 🔴 High | 0.5 days + email template |
| **No annual billing option** — monthly-only pricing in current Plan model | 🟡 Medium | 1 day |
| **No coupon / promo code flow** | 🟢 Low | 1 day |
| **`STRIPE_WEBHOOK_SECRET` must be set** for signature verification; empty default would accept forged events | 🔴 Critical | Config + deploy |
| **No referral / affiliate tracking** | 🟢 Low | 3rd-party tool |

---

## 4. API Surface (Django Ninja)

### What Exists ✅
- Django Ninja OpenAPI at `/api/resume/` with automatic docs at `/api/resume/docs`
- Session auth as primary; `CustomerApiKey` header auth as secondary
- Per-plan `api_access` flag gates API feature availability
- `LLM_COMPLETE_MAX_INPUT_CHARS` cap on the free-form LLM completion endpoint
- `AbuseThrottleMiddleware` covers auth (20 req / 5 min) and API (120 req / 60s) paths
- Structured error responses with proper HTTP status codes
- Rate limiting via Redis per-provider (RPM + TPM) for cloud LLM calls

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No API versioning** — no `/v1/` prefix; breaking changes would break all integrations | 🟡 Medium | 1 day |
| **No per-API-key rate limiting** — throttle is per-IP, not per API key; one key could exhaust limits | 🟡 Medium | 1 day |
| **`/api/resume/docs` is publicly accessible** — API documentation is open by default | 🟡 Medium | 1 hour |
| **`API_ACCESS_TOKEN` simple auth is deprecated** and co-exists with CustomerApiKey | 🟢 Low | Cleanup |
| **No webhook delivery to customers** — no way for customers to subscribe to events (job match, apply complete) | 🟡 Medium (for B2B) | 3+ days |

---

## 5. Security

### What Exists ✅
- `SECURE_SSL_REDIRECT` → auto-enabled when `DEBUG=False`
- `SESSION_COOKIE_SECURE` + `CSRF_COOKIE_SECURE` auto-enabled in production
- `SECURE_HSTS_SECONDS = 31536000` in production
- `SecurityMiddleware` + `XFrameOptionsMiddleware`
- `CsrfViewMiddleware` on all forms
- Fernet encryption for stored API keys (`crypto.py`) with key rotation support (comma-separated `FERNET_KEYS`)
- SMTP credentials in `SiteCredential` encrypted at rest
- `django-hijack` with custom permission checks + audit log
- `AbuseThrottleMiddleware` for login and API rate limiting
- SQL injection protection via Django ORM (no raw queries observed)
- All `BigAutoField` PKs (no sequential integer ID enumeration risk from tiny IDs)
- Auth4xx / 403 / 404 / 500 custom error pages

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **`SECRET_KEY` has insecure default** — `django-insecure-+a#...` hardcoded; must be replaced before deploy | 🔴 Critical | Config + deploy |
| **`ALLOWED_HOSTS` defaults to `[]`** — Django allows all hosts when empty in DEBUG; must be set in production | 🔴 Critical | Config + deploy |
| **`DEBUG=True` default** — must be `False` in production environment; currently relies on operator discipline | 🔴 Critical | Config / deploy checklist |
| **No Content Security Policy (CSP) header** — XSS mitigation is limited to Django template auto-escaping | 🟡 Medium | 1 day (`django-csp`) |
| **`STRIPE_WEBHOOK_SECRET` empty default** — webhook signature verification skipped; forged events possible | 🔴 Critical | Config |
| **No security.txt / responsible disclosure** | 🟢 Low | 0.5 hours |
| **Tailwind CSS loaded from CDN** — no SRI hash; CDN compromise injects arbitrary JS | 🟡 Medium | 1 day (bundle locally) |
| **`browser-use` runs arbitrary AI-generated browser actions** — prompt injection in job descriptions could trigger unintended automation | 🔴 High | Sandbox / domain allowlist |
| **No audit log for data export** — `export_account_json` leaves no trail | 🟡 Medium | 0.5 days |

---

## 6. Scalability & Infrastructure

### What Exists ✅
- Huey `RedisExpireHuey` task queue with configurable Redis URL, TLS, password
- `HUEY_IMMEDIATE=1` for dev (no Redis required locally)
- MySQL/MariaDB support with TLS in production; SQLite + WAL for dev
- S3-compatible media storage (`django-storages` + boto3)
- `LLM_USER_MAX_CONCURRENT = 2` per-user LLM slot limiting
- `APPLY_BROWSER_CONCURRENCY = 2` cap on simultaneous Playwright browsers
- `CONN_MAX_AGE=60` database connection pooling
- Task result TTL (`HUEY_RESULT_EXPIRE_SECONDS = 86400`) to avoid unbounded growth

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **Single shared Huey queue** — no priority lanes; a bulk scrape job starves interactive resume optimization | 🔴 High | 1–2 days (separate queue names) |
| **Global Redis lock for job search** — `JOB_SEARCH_TASK_LOCK_KEY` blocks all users when one is searching | 🔴 High | 1 day (tenant-scoped lock key) |
| **Only 2 Huey worker threads** — default `workers: 2` is too few for a multi-user SaaS | 🟡 Medium | Config change |
| **No horizontal scaling story for Playwright** — browser workers run on the same host; can't distribute | 🟡 Medium | Architecture decision |
| **No distributed session store** — session backend is Django default (DB table); works for single-node | 🟡 Medium | Redis sessions for multi-node |
| **`StaticFiles` not configured for CDN** — `STATIC_URL = "static/"` without `WhiteNoise` or CDN | 🟡 Medium | `whitenoise` or S3 |
| **No health check endpoint** — no `/health/` or `/ready/` URL for load balancer / Kubernetes | 🟡 Medium | 0.5 days |
| **No observability / tracing** — no Sentry, no structured logging, no APM | 🔴 High | 1–2 days (Sentry SDK) |
| **SQLite in production** — WAL mode helps but SQLite doesn't support concurrent writes at scale | 🔴 Critical if dev DB used | MySQL migration (already scripted) |

---

## 7. LLM Cost Governance

### What Exists ✅
- `check_token_budget()` and `consume_token_budget()` enforced before every cloud LLM call
- Per-plan daily token budgets (`LLM_DAILY_TOKEN_LIMIT_BY_PLAN`)
- Platform-wide shared burn cap (`LLM_PLATFORM_DAILY_TOKEN_LIMIT = 5,000,000`)
- Per-user concurrency limiting (`LLM_USER_MAX_CONCURRENT`)
- Provider-level rate limiting (RPM + TPM) via Redis sliding window
- Local Ollama preference for bulk operations (vetting, JD cleansing, seniority) — prevents cloud cost spikes
- `only_local=True` enforcement for automated matching pipeline
- `LLM_DAILY_TOKEN_LIMIT_BY_PLAN` keyed to plan slug; configurable via env vars
- Daily usage ledger (`UsageCounter` + new `LLMDailyUsageBreakdown`) with per-query breakdown
- Informative error messages identifying plan name, tokens used vs limit, and remediation steps
- Admin "Reset today's quota" button + staff reset endpoint

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No real-time cost alert** — no email/notification when a user approaches 80% of platform budget | 🟡 Medium | 1 day |
| **No per-request cost estimate displayed to user before generation** | 🟢 Low | 1 day |
| **Platform key sharing is not zero-sum** — if one power user burns the platform daily budget, all users get blocked | 🔴 High | Per-user platform key accounting already partially in place; needs hard per-user cap |
| **No cost dashboard for operator** — no total $ spend visible in admin | 🟡 Medium | 2 days |

---

## 8. Onboarding & UX

### What Exists ✅
- `landing.html` — public marketing page with hero, features, pricing section
- `getting_started.html` — onboarding checklist (resume → LLM → search profile → find jobs)
- `_onboarding_banner.html` — contextual "complete setup" nudge in app
- Email verification flow with resend ability
- `legal/terms.html` and `legal/privacy.html` — placeholder pages with counsel note
- `billing.html` — plan selection page with Stripe Checkout integration
- `getting_started.html` tracks completion progress per step

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **Legal pages are placeholders** — explicitly note "Replace this placeholder with counsel-reviewed language before a public launch" | 🔴 Critical | Legal review + copy |
| **No in-app guided tour** — no interactive walkthrough for new users | 🟡 Medium | 2–3 days |
| **No transactional email pipeline** — `EMAIL_BACKEND` defaults to console; production email not wired | 🔴 Critical | SES/Mailgun config |
| **No welcome email on signup** | 🔴 High | 0.5 days + SMTP config |
| **No help documentation / FAQ** | 🟡 Medium | Content work |
| **No changelog / release notes** | 🟢 Low | Content work |
| **No in-app support channel** (chat widget, Crisp, Intercom) | 🟡 Medium | 0.5 days integration |

---

## 9. GDPR / Legal / Compliance

### What Exists ✅
- `export_account_json()` — GDPR data portability: exports resumes, job pipeline, API keys, settings to JSON
- `delete_user_account()` with username + password confirmation — right to erasure
- Privacy policy page (placeholder, requires legal review)
- Terms of service page (placeholder, requires legal review)
- Stripe only stores billing identifiers, not full card data

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **Privacy/Terms are placeholder text** — legally unenforceable | 🔴 Critical | Legal counsel |
| **No cookie consent banner** — Tailwind CDN, potentially analytics could trigger ePrivacy | 🟡 Medium | 1 day |
| **No data retention policy enforcement** — `cleanup_manager` purges job data but no documented data retention SLA | 🟡 Medium | Policy + implementation |
| **No GDPR-specific "right to access" request workflow** — self-serve export exists but no formal request tracking | 🟡 Medium | 1 day |
| **No data processing agreement (DPA)** for B2B / EU customers | 🔴 High (EU) | Legal |

---

## 10. Operational Readiness

### What Exists ✅
- `django-hijack` staff impersonation for customer support
- `admin_billing.html` admin billing view (user list, plan status, usage)
- Staff admin panel (Django admin) with all models registered
- `ImpersonationAuditLog` for compliance trail
- `LLMDailyUsageBreakdown` granular query-level cost tracking
- Per-tenant usage in admin billing view
- Huey dashboard at `/dev/huey/` (dev-only)

### What Is Missing / Incomplete ⚠️
| Gap | Risk | Effort |
|-----|------|--------|
| **No error monitoring** — no Sentry or similar; production errors invisible to operators | 🔴 Critical | 0.5 days |
| **Huey monitor is dev-only** — no production task queue visibility | 🟡 Medium | Flower or Huey web UI |
| **No alerting on task queue backup** | 🟡 Medium | Prometheus / Datadog |
| **No customer-facing status page** | 🟡 Medium | Statuspage.io |
| **No backup strategy documented** — SQLite DB in `db.sqlite3` on local disk | 🔴 Critical | MySQL + daily dumps |

---

## Priority Matrix: What to Fix Before Launch

### 🔴 Critical (Launch Blockers)

| # | Item | Effort |
|---|------|--------|
| 1 | Replace `SECRET_KEY` default + enforce via env validation at startup | 2 hours |
| 2 | Set `STRIPE_WEBHOOK_SECRET` + validate signature on every webhook | 1 hour |
| 3 | Replace placeholder Privacy Policy + Terms with counsel-reviewed text | Legal |
| 4 | Configure transactional email (SES / Mailgun / Postmark) | 1 day |
| 5 | Set `DEBUG=False` + `ALLOWED_HOSTS` in production config; add startup assertion | 2 hours |
| 6 | Error monitoring — Sentry SDK installation + DSN config | 2 hours |
| 7 | Migrate production to MySQL (not SQLite) | 1 day |

### 🔴 High (Week 1 After Soft Launch)

| # | Item | Effort | Status |
|---|------|--------|--------|
| 8 | Enforce email verification before first LLM call | 1 day | ✅ **Completed** |
| 9 | Tenant-scoped Redis job search lock (interactive & background) | 1 day | ✅ **Completed** |
| 10 | Failed payment email notification | 0.5 days | Pending |
| 11 | Priority queue separation (interactive vs. batch) | 2 days | Pending |
| 12 | Sandbox browser-use / Apply Agent to domain allowlist | 2 days | Pending |
| 13 | Rotate any shared Playwright/scraping IPs via proxy | Architecture | Pending |

### 🟡 Medium (Month 1)

| # | Item | Effort |
|---|------|--------|
| 14 | 2FA / TOTP (`django-otp`) | 2–3 days |
| 15 | Google/GitHub OAuth | 2–3 days |
| 16 | Content Security Policy header | 1 day |
| 17 | Bundle Tailwind locally (remove CDN) | 0.5 days |
| 18 | Health check endpoint | 0.5 days |
| 19 | In-app support widget | 0.5 days |
| 20 | Free trial period flow | 1 day |
| 21 | API versioning | 1 day |
| 22 | Cookie consent banner | 1 day |

---

## Summary Scorecard

| Dimension | Score | Status |
|-----------|-------|--------|
| Core Product Functionality | 9/10 | ✅ Excellent |
| Authentication & Identity | 6/10 | ⚠️ No 2FA, email not enforced |
| Billing & Monetization | 7/10 | ⚠️ Missing trial, payment emails |
| Security Hardening | 6/10 | ⚠️ Default secrets, no CSP |
| Multi-Tenancy | 5/10 | ⚠️ App-level only, global locks |
| LLM Cost Control | 8/10 | ✅ Strong governance |
| Scalability | 6/10 | ⚠️ Single queue, no observability |
| Onboarding / UX | 7/10 | ⚠️ No email, no tour |
| Legal / Compliance | 4/10 | 🔴 Placeholder legal pages |
| Operations | 5/10 | 🔴 No error monitoring, no backups |
| **Overall** | **6.3/10** | **Soft Launch Possible; Hard Launch Needs ~3–4 weeks** |

---

## Addendum: 10 Non-Obvious Findings from Deep Code Review

These are subtle issues that wouldn't surface from a surface-level review but have real SaaS implications.

### 1. 🟢 `SAAS_STAFF_BYPASS_QUOTAS` defaults to `False` — a positive surprise
Most SaaS codebases silently exempt staff from quotas, creating runaway internal LLM usage on platform keys. Here it's explicitly opt-in (`default=False`), which is correct and safe. Worth preserving and calling out in your security model.

### 2. 🟡 `UserPromptProfile` is a dead model — schema debt
The model still exists in `models.py`, still has a DB table, but its docstring explicitly says *"No longer used at runtime; retained for historical data."* This is silent schema debt that could confuse developers and security auditors. Should be dropped or formally documented as deprecated.

### 3. 🔴 Daily token limits are **soft caps**, not hard limits
`check_token_budget()` runs pre-flight, but if a call is within budget, the LLM call proceeds, and the actual token overshoot post-invoke is only *logged*, not raised. A user at 199,900/200,000 tokens can trigger a 5,000-token call and the overshoot silently succeeds. This is intentional design (to avoid discarding a completed response), but operators should know the limits are approximate ±1 call.

### 4. 🔴 Abuse throttle doesn't share state under multi-worker Gunicorn without Redis
`AbuseThrottleMiddleware` uses `django.core.cache`. In dev with `HUEY_IMMEDIATE=True`, this is `LocMemCache` — *per-process*. If Gunicorn is run with multiple workers in production without Redis cache configured (`DJANGO_CACHE_USE_REDIS` or a `DJANGO_CACHE_URL`), the throttle won't share state across workers. An attacker can distribute brute-force across all workers simultaneously. **Mitigation**: ensure `DJANGO_CACHE_USE_REDIS=1` is set in production.

### 5. 🔴 `LLM_RATE_LIMIT_FAIL_OPEN` is tied to `DEBUG`
If `DEBUG=True` is accidentally left on in production (a very common misconfiguration), both the LLM concurrency limiter and provider rate limiter silently fail open — meaning Redis outages or misconfigurations allow unlimited platform LLM consumption. **Add a startup assertion**: `assert not (DEBUG and not LLM_RATE_LIMIT_FAIL_OPEN) or warn loudly`.

### 6. 🔴 `OwnedManager` does NOT override `get_queryset()` — all protection is convention-based
`Model.objects.all()` and `Model.objects.filter(pk=...)` work without owner filtering. The security boundary exists only because developers consistently call `.for_user(user)`. Any view or API endpoint that skips this call is an IDOR vulnerability. **This is the single largest architectural trust concern for multi-tenant safety.** Mitigation: code review checklist + automated test that verifies every view returns HTTP 403/404 when authenticated as a different user.

### 7. 🟡 `AtsJudgeProfile` and `OptimizerWorkflow` use `owner=null` for global rows
Staff-managed global rows and per-user rows coexist in the same table, differentiated by `owner IS NULL`. The `OwnedManager.for_user()` path would return `none()` for null-owner global rows, requiring a parallel lookup (probably direct `filter(owner__isnull=True)`) that bypasses the tenancy layer. If this parallel path is ever misused in a new view, global rows could be mutated by non-staff users.

### 8. 🟡 `JobListing` is globally shared — GDPR deletion doesn't purge scraped job data
`JobListing` has no `owner` FK. When a user deletes their account via `delete_user_account()`, the job listings they triggered fetching (including `description`, `raw_json`, potentially with contact names) remain permanently in the global table. This is technically defensible (the listings aren't "their" data) but may need to be addressed in the Privacy Policy's "data we retain" section.

### 9. 🟡 `APPLY_BROWSER_HEADLESS` defaults to `False` — a production footgun
If a worker server is deployed without explicitly setting `APPLY_BROWSER_HEADLESS=1`, the apply agent will attempt to open visible Chromium windows on a headless server, causing Playwright failures. This should default to `True` in production and only be overridden to `False` explicitly for local development.

### 10. 🟡 Stripe webhook path must exactly match `LOGIN_EXEMPT_URL_PREFIXES`
The webhook endpoint at `/billing/stripe/webhook/` is CSRF-exempt and login-exempt. If the Stripe dashboard webhook URL is misconfigured to a slightly different path (e.g., missing trailing slash), Django's CSRF middleware would reject delivery with a 403, and the exemption path would silently not apply. **Add this to the deployment checklist**: verify Stripe dashboard webhook URL matches exactly `/billing/stripe/webhook/`.

