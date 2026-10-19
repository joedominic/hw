# LLM Gateway And Policy

Canonical reference for how product LLM traffic is selected, limited, timed out, and recorded.

## Modules

| Module | Role |
|--------|------|
| [`llm/gateway.py`](../llm/gateway.py) | Central LangChain invoke path: kill switch, preference order, job pin, failover, usage recording, token budget consume, Langfuse metadata enrichment |
| [`llm/policy.py`](../llm/policy.py) | Shared policy: kill switch helper, daily token budgets, per-user concurrency, invoke timeout, browser-use LLM wrapper |
| [`llm/rate_limit.py`](../llm/rate_limit.py) | Redis RPM/TPM slots and cooldowns (BYOK vs platform scope) |
| [`llm/factory.py`](../llm/factory.py) | LiteLLM abstraction (`LiteLLMChatModel`), multi-provider parameter mappings, and Langfuse callback configuration |
| [`llm/pii.py`](../llm/pii.py) | PII masking and redaction utilities (scans emails, phones, SSNs, addresses, and social handles) |
| [`llm/session.py`](../llm/session.py) | Active provider + runtime candidates (UI / apply-agent candidate pick) |
| [`llm/services.py`](../llm/services.py) | Provider models & connectivity probes |
| [`rate_limits.py`](../rate_limits.py) | Plan daily **request** quota bridge (`METRIC_LLM_REQUESTS`) |
| [`subscriptions/entitlements.py`](../subscriptions/entitlements.py) | Plans, `UsageCounter`, including `METRIC_LLM_TOKENS` |

**Entry points**

- LangChain product calls: `resume_app.llm.invoke_llm_messages` / `resume_app.llm.call_invoke_llm_messages`
- Non-LangChain (browser-use): `resume_app.llm.wrap_browser_use_llm`
- Do **not** call `get_llm(...).invoke(...)` for product traffic except connect/validation pings

## Call flow

```
Caller (agents, jobs_api, job_prep, pipeline extract, api)
  → check request quota + kill switch
  → check daily token budget (user + platform if env keys)
  → consume 1× llm_requests
  → acquire per-user concurrency slot
  → [selection path] ordered preferences, pin, RPM/TPM, failover
  → [override path] llm_override + acquire_llm_slot
  → invoke with wall-clock timeout
  → record LLMAppUsage* + consume llm_tokens
```

Apply-agent generic fill: 1× `llm_requests` at run start; each browser-use turn goes through the policy wrapper (tokens, concurrency, timeout, usage, kill switch).

## Who must use the gateway

| Path | Routing |
|------|---------|
| Optimizer / Writer / ATS / Recruiter / optimizer JD-cleanse step | `agents` → gateway **cloud-only** (`allow_local=False`) |
| Pipeline JD cleanse (`JDCleanserService`) | Prefer **Ollama Local** (`prefer_local=True`), cloud fallback; heuristic fallback |
| Vetting match / fit check / other non-optimizer product LLM | Prefer **Ollama Local** (`prefer_local=True`) |
| Job prep, job insights, AI match | Prefer local via gateway defaults |
| Pipeline skill extract + consolidate | `pipeline_llm_skill_extract` → gateway (prefer local) |
| `POST /llm/complete` | gateway (requires plan `api_access`; size-capped) |
| Apply-agent browser-use | `wrap_browser_use_llm` (policy; dedicated apply LLM setting) |
| Settings connect / Ollama guard | Direct `get_llm` ping only (not product metering) |

## Quotas And budgets

Enforced when `SAAS_ENFORCE_QUOTAS=True` (off automatically during `manage.py test`).

| Control | Metric / store | Defaults |
|---------|----------------|----------|
| Daily LLM requests | `UsageCounter` / `llm_requests` | Free 50, Pro 500, Unlimited 0; optional `LLM_USER_DAILY_REQUEST_LIMIT` ceiling |
| Daily LLM tokens | `UsageCounter` / `llm_tokens` | Free 200k, Pro 2M via `LLM_DAILY_TOKEN_LIMIT_*`; optional `LLM_USER_DAILY_TOKEN_LIMIT` |
| Platform key burn | Redis `llm:platform:tok:v1:{date}` | `LLM_PLATFORM_DAILY_TOKEN_LIMIT` (default 5M); ops-only, not on user UI |
| Kill switch | `AppAutomationSettings.stop_llm_requests` | Per-user |

Token budget check is **preflight** (estimate); consume is **after** success (input+output). Overshoot after success is logged, not used to drop the response.

## Rate limits (provider RPM/TPM)

- Redis keys scoped `u{user_id}` for BYOK, `platform` for env keys.
- `LLM_RATE_LIMIT_BY_PROVIDER` defaults: **Groq** and **OpenAI**.
- Preference-row RPM/TPM override env defaults when both set.
- `LLM_RATE_LIMIT_FAIL_OPEN` defaults to **`DEBUG`** (fail-closed when `DEBUG=False`). Confirm prod `.env` does not force `True`.
- Multi-candidate gateway path never fail-opens a full bucket (`prefer_failover=True`).

## Concurrency And timeouts

| Setting | Default | Behavior |
|---------|---------|----------|
| `LLM_USER_MAX_CONCURRENT` | 2 | Redis in-flight per user; 0 = unlimited |
| `LLM_INVOKE_TIMEOUT_SECONDS` | 180 | Wall-clock wait; 0 = disabled |

Timeout uses a worker thread + `Future.result(timeout=)` (or `asyncio.wait_for` for browser-use `ainvoke`). The waiter returns; the underlying HTTP call may still finish in the background.

## Usage visibility

| UI | What it shows |
|----|----------------|
| Settings → Usage **Today’s plan limits** | Daily `llm_requests` + `llm_tokens` used / limit / remaining |
| Settings → Usage **Lifetime** | `LLMAppUsageTotals`, by-query (`USAGE_QUERY_*`), by-model — all-time analytics |
| Billing | Same daily metrics (labeled) + plan card token/day limits |

Lifetime analytics and daily quotas are different stores. Both are required for ops vs entitlement clarity.

Query kinds include optimizer steps, job search, pipeline vetting/skill extract/JD cleanse, apply-agent, and API complete. See `USAGE_QUERY_LABELS` in `llm_gateway.py`.

## API surface

`POST /api/resume/llm/complete`

- Requires plan `api_access` (Pro+).
- Combined system+user length ≤ `LLM_COMPLETE_MAX_INPUT_CHARS` (default 16000).
- Errors: `403` entitlement, `429` request/token/concurrency, `503` kill switch, `504` timeout.

## Selection And pinning

When `llm_override` is unset:

1. Build candidates from `LLMProviderPreference` (connected keys).
2. Skip models on cooldown.
3. Prefer local when `prefer_local=True` (pipeline JD cleanse, vetting match, and other non-optimizer paths). Resume-optimizer nodes pass `prefer_local=False` + `allow_local=False` (**cloud only**).
4. Resume optimization sets `allow_local=False`: **Ollama Local is never used in the optimizer graph**; if no cloud candidate remains, the run fails with `LLMUnavailableError` / `NO_CLOUD_LLM_MESSAGE`.
5. `Ollama Local` is always treated as local even if the preference row’s `is_local` flag was left unchecked.
6. Pin successful provider/model to `job_cache_key` in Redis (2-day TTL). A local pin is ignored when the caller asked for remote-first / disallow-local and remotes exist.
7. On 429/quota: cooldown, clear pin, try next candidate.

When `llm_override` is set: selection/pin skipped; request quota, token budget, concurrency, timeout, and RPM/TPM still apply. Overrides that point at Ollama Local are rejected when `allow_local=False`.

Every invoke logs at WARNING: `[llm] invoking {provider}/{model} (query=… via=gateway|gateway-override|browser-use|direct)`. Gateway selection also logs candidate order before the first attempt.

## Exceptions

| Exception | Meaning |
|-----------|---------|
| `LLMRequestsDisabled` | Kill switch on |
| `LLMUserRateLimitExceeded` | Daily request quota |
| `LLMTokenBudgetExceeded` | Daily user or platform token budget |
| `LLMConcurrencyLimitExceeded` | Too many in-flight calls |
| `LLMInvokeTimeout` | Wall-clock deadline exceeded |

## Observability & Telemetry (Langfuse Cloud)

All gateway invocations automatically stream traces to **Langfuse Cloud** via LiteLLM's callback hooks (`success_callback` and `failure_callback`).

- **Activation:** Automatically configured on startup in `ResumeAppConfig.ready()` via `configure_langfuse_telemetry()`.
- **Enriched Metadata & Trace Naming:** Calls resolve descriptive trace and generation names via `resolve_trace_names` in `resume_app.llm.telemetry`. Multi-step optimizer calls group under `trace.name = "resume_optimizer"` with distinct generation names (`optimizer_writer`, `optimizer_ats_judge`, `optimizer_recruiter_judge`, `jd_cleanse`). Standalone calls log specific trace names (`fit_check`, `job_matching`, `pipeline_skill_extract`). Every call attaches user/tenant ID, model, provider, and `session_id = job_cache_key`.
- **Privacy & PII Guardrails:**
  - `LANGFUSE_MASK_PII=True` (default): **Selective PII Redaction**. Automatically masks candidate/user names, physical addresses, emails, phone numbers, SSNs, and social handles (`[NAME]`, `[ADDRESS]`, `[EMAIL]`, `[PHONE]`, `[SSN]`, `[LINKEDIN]`, `[GITHUB]`) before export, while preserving full prompt context, job descriptions, ATS scoring, and evaluation text for monitoring.
  - `LANGFUSE_REDACT_CONTENT=False` (default): Emergency zero-content mode. When set to `True`, completely blanks out prompts and responses with `"redacted-by-litellm"`. Use only for extreme compliance requirements.
- **Ollama Reasoning & Think Mode Guardrail:**
  - Local reasoning models (e.g., `hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M`, DeepSeek-R1) prefill `<think>` prompt templates in Ollama, using an internal chain-of-thought scratchpad to achieve high reasoning accuracy on complex evaluations.
  - HireEdge enables think mode by default (`OLLAMA_ENABLE_THINKING=True`) to maintain maximum evaluation accuracy. Any leaked `</think>` tags in output content are automatically split, keeping the clean answer in `content` and routing the thought monologue to `additional_kwargs["reasoning_content"]` (and Langfuse).
  - To prevent read timeouts on large prompts during local reasoning generation, `LLM_INVOKE_TIMEOUT_SECONDS` is set to `300` (5 minutes), and system prompts include a concise reasoning directive (<250 tokens) to curb speculative wandering. If speed is prioritized over reasoning depth, set `OLLAMA_ENABLE_THINKING=False` to force `think=False`.

## Tests And env

- Tests: `resume_app.test_llm_policy`, entitlement token cases in `test_saas_phase2`.
- Env template: root `.env.example` (`LLM_RATE_LIMIT_*`, `LLM_DAILY_TOKEN_*`, `LLM_INVOKE_*`, `LLM_COMPLETE_*`).
- Related ops: root [`docs/operations.md`](../../../docs/operations.md).
