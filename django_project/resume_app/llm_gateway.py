"""
Central LLM invoke path: kill switch, preference order, job pinning, cooldowns, usage stats.

Canonical docs: ``resume_app/docs/LLM_GATEWAY.md`` (quotas, policy, concurrency, timeouts).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from django.db.models import F
from django.utils import timezone

from .crypto import decrypt_api_key
from .llm_factory import get_llm
from .llm_policy import (
    LLMConcurrencyLimitExceeded,
    LLMInvokeTimeout,
    LLMRequestsDisabled,
    LLMTokenBudgetExceeded,
    check_token_budget,
    consume_token_budget,
    run_with_invoke_timeout,
    user_llm_concurrency,
)
from .llm_rate_limit import (
    acquire_llm_slot,
    estimate_tokens_from_messages,
    get_cooldown_seconds_for_provider_model,
    is_llm_on_cooldown,
    set_llm_cooldown,
    try_acquire_llm_slot,
)
from .models import LLMProviderPreference, LLMAppUsageTotals, LLMUsageByModel, LLMUsageByQuery

logger = logging.getLogger(__name__)

# Re-export for callers that import kill-switch from the gateway.
__all__ = [
    "LLMRequestsDisabled",
    "LLMTokenBudgetExceeded",
    "LLMConcurrencyLimitExceeded",
    "LLMInvokeTimeout",
    "LLMUnavailableError",
    "NO_CLOUD_LLM_MESSAGE",
    "invoke_llm_messages",
    "call_invoke_llm_messages",
    "log_llm_invoke",
    "provider_is_local",
    "cloud_llm_available",
]

# Keys for `usage_query_kind` / `LLMUsageByQuery.query_kind` (Settings → Usage labels in USAGE_QUERY_LABELS).
USAGE_QUERY_FIT_CHECK = "fit_check"
USAGE_QUERY_MATCHING = "matching"
USAGE_QUERY_OPTIMIZER_WRITER = "optimizer_writer"
USAGE_QUERY_OPTIMIZER_ATS_JUDGE = "optimizer_ats_judge"
USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE = "optimizer_recruiter_judge"
USAGE_QUERY_JOB_INSIGHTS = "job_insights"
USAGE_QUERY_COVER_LETTER = "cover_letter"
USAGE_QUERY_INTERVIEW_PREP = "interview_prep"
USAGE_QUERY_JOBS_AI_MATCH = "jobs_ai_match"
USAGE_QUERY_KEYWORD_SEARCH_FIT = "keyword_search_fit"
USAGE_QUERY_JOBS_MATCH_API = "jobs_match_api"
USAGE_QUERY_PIPELINE_VETTING = "pipeline_vetting_matching"
USAGE_QUERY_PIPELINE_RESUME_REFINE = "pipeline_resume_refine"
USAGE_QUERY_PIPELINE_SKILL_EXTRACT = "pipeline_skill_extract"
USAGE_QUERY_JD_CLEANSE = "jd_cleanse"
USAGE_QUERY_APPLY_AGENT = "apply_agent"
USAGE_QUERY_API_LLM_COMPLETE = "api_llm_complete"
USAGE_QUERY_API_RESUME_FIT = "api_resume_fit"
USAGE_QUERY_UNSPECIFIED = "unspecified"

USAGE_QUERY_LABELS: dict[str, str] = {
    USAGE_QUERY_UNSPECIFIED: "Other / not labeled",
    USAGE_QUERY_FIT_CHECK: "Fit check",
    USAGE_QUERY_MATCHING: "Job matching",
    USAGE_QUERY_OPTIMIZER_WRITER: "Resume optimizer — writer",
    USAGE_QUERY_OPTIMIZER_ATS_JUDGE: "Resume optimizer — ATS judge",
    USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE: "Resume optimizer — recruiter judge",
    USAGE_QUERY_JOB_INSIGHTS: "Job search — batch insights",
    USAGE_QUERY_COVER_LETTER: "Optimizer — cover letter",
    USAGE_QUERY_INTERVIEW_PREP: "Done — interview prep",
    USAGE_QUERY_JOBS_AI_MATCH: "Job search — AI match",
    USAGE_QUERY_KEYWORD_SEARCH_FIT: "Job search — keyword search fit",
    USAGE_QUERY_JOBS_MATCH_API: "Job search — single job fit (API)",
    USAGE_QUERY_PIPELINE_VETTING: "Pipeline — vetting match",
    USAGE_QUERY_PIPELINE_RESUME_REFINE: "Pipeline — resume keyword refine",
    USAGE_QUERY_PIPELINE_SKILL_EXTRACT: "Pipeline — resume skill extract",
    USAGE_QUERY_JD_CLEANSE: "Pipeline — JD cleanse",
    USAGE_QUERY_APPLY_AGENT: "Apply agent — browser-use LLM",
    USAGE_QUERY_API_LLM_COMPLETE: "HTTP API — LLM complete",
    USAGE_QUERY_API_RESUME_FIT: "HTTP API — resume fit (multipart)",
}

PIN_PREFIX = "llm:pin:v1:"
RR_PREFIX = "llm:rr:v1:"
PIN_TTL_SECONDS = 86400 * 2

# Shown when optimizer (or other allow_local=False callers) have no remote candidates.
NO_CLOUD_LLM_MESSAGE = (
    "No cloud LLM available. Connect a remote provider in Settings "
    "(Groq, OpenAI, Anthropic, Gemini, Ollama Cloud, etc.). "
    "Ollama Local cannot be used for resume optimization."
)


class LLMUnavailableError(RuntimeError):
    """Raised when no eligible (usually cloud) LLM candidates remain for a call."""


def provider_is_local(provider: str | None, *, preference_is_local: bool = False) -> bool:
    """True for preference-marked local rows and inherent local providers (Ollama Local)."""
    if preference_is_local:
        return True
    name = (provider or "").strip().lower()
    return name == "ollama local"


def log_llm_invoke(
    provider: str | None,
    model: str | None,
    *,
    query: str | None = None,
    via: str = "gateway",
    extra: str = "",
) -> None:
    """Log provider/model for every product LLM call (gateway, browser-use, direct pings)."""
    logger.warning(
        "[llm] invoking %s/%s (query=%s via=%s%s)",
        (provider or "").strip() or "unknown",
        (model or "").strip() or "default",
        query or USAGE_QUERY_UNSPECIFIED,
        via,
        f" {extra}" if extra else "",
    )


def _redis_client():
    from .llm_rate_limit import _get_redis

    return _get_redis()


def _is_rate_limit_error(exc: BaseException | None) -> bool:
    """True if this or a chained cause looks like quota / 429 (provider-agnostic)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur)
        name = type(cur).__name__
        if (
            "429" in msg
            or "ResourceExhausted" in name
            or ("rate" in msg.lower() and "limit" in msg.lower())
            or "quota" in msg.lower()
        ):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False


def normalize_usage_model_key(provider: str, model: str | None) -> tuple[str, str]:
    p = (provider or "").strip()
    m = (model or "").strip()
    return p, (m if m else "__default__")


def record_llm_usage(
    provider: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    tokens_estimated: bool,
    *,
    user,
    query_kind: str | None = None,
) -> None:
    prov, mkey = normalize_usage_model_key(provider, model)
    try:
        totals = LLMAppUsageTotals.get_for_user(user)
        LLMAppUsageTotals.objects.filter(pk=totals.pk).update(
            total_input_tokens=F("total_input_tokens") + max(0, int(input_tokens)),
            total_output_tokens=F("total_output_tokens") + max(0, int(output_tokens)),
            total_requests=F("total_requests") + 1,
            total_estimated_invokes=F("total_estimated_invokes") + (1 if tokens_estimated else 0),
        )
    except Exception as e:
        logger.warning("record_llm_usage totals failed: %s", e)
    try:
        row, _created = LLMUsageByModel.objects.get_or_create(
            owner=user,
            provider=prov,
            model=mkey,
            defaults={
                "request_count": 0,
                "sum_input_tokens": 0,
                "sum_output_tokens": 0,
                "sum_cached_tokens": 0,
            },
        )
        LLMUsageByModel.objects.filter(pk=row.pk).update(
            request_count=F("request_count") + 1,
            sum_input_tokens=F("sum_input_tokens") + max(0, int(input_tokens)),
            sum_output_tokens=F("sum_output_tokens") + max(0, int(output_tokens)),
            sum_cached_tokens=F("sum_cached_tokens") + max(0, int(cached_tokens)),
            last_used_at=timezone.now(),
        )
    except Exception as e:
        logger.warning("record_llm_usage by-model failed: %s", e)
    qk = ((query_kind or "").strip() or USAGE_QUERY_UNSPECIFIED)[:64]
    try:
        qrow, _ = LLMUsageByQuery.objects.get_or_create(
            owner=user,
            query_kind=qk,
            provider=prov,
            model=mkey,
            defaults={
                "request_count": 0,
                "sum_input_tokens": 0,
                "sum_output_tokens": 0,
                "sum_cached_tokens": 0,
            },
        )
        LLMUsageByQuery.objects.filter(pk=qrow.pk).update(
            request_count=F("request_count") + 1,
            sum_input_tokens=F("sum_input_tokens") + max(0, int(input_tokens)),
            sum_output_tokens=F("sum_output_tokens") + max(0, int(output_tokens)),
            sum_cached_tokens=F("sum_cached_tokens") + max(0, int(cached_tokens)),
            last_used_at=timezone.now(),
        )
    except Exception as e:
        logger.warning("record_llm_usage by-query failed: %s", e)


def _preference_candidates(user) -> list[dict]:
    rows = (
        LLMProviderPreference.objects.select_related("provider_config")
        .filter(
            provider_config__owner=user,
            provider_config__encrypted_api_key__isnull=False,
        )
        .exclude(provider_config__encrypted_api_key="")
        .order_by("priority", "id")
    )
    out = []
    seen = set()
    for row in rows:
        cfg = row.provider_config
        prov = (cfg.provider or "").strip()
        if not prov:
            continue
        raw_model = (row.model or cfg.default_model or "").strip()
        resolved_get_llm = raw_model or None
        mkey = raw_model if raw_model else "__default__"
        key = (prov, mkey)
        if key in seen:
            continue
        seen.add(key)
        api_key = decrypt_api_key(cfg.encrypted_api_key or "")
        if not (api_key or "").strip():
            continue
        out.append(
            {
                "provider": prov,
                "model_get_llm": resolved_get_llm,
                "model_key": mkey,
                "priority": int(row.priority),
                "is_local": provider_is_local(prov, preference_is_local=bool(row.is_local)),
                "preference_id": row.id,
                "config": cfg,
                "api_key": api_key,
            }
        )
    return out


def preference_candidates_available(user) -> bool:
    return bool(_preference_candidates(user))


def cloud_llm_available(user) -> bool:
    """True when at least one non-local preference candidate is configured and not on cooldown."""
    for c in _preference_candidates(user):
        if c.get("is_local"):
            continue
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"], user=user):
            continue
        return True
    return False


def local_llm_available(user) -> bool:
    """True when at least one local preference candidate (e.g. Ollama) is configured and not on cooldown."""
    for c in _preference_candidates(user):
        if not c.get("is_local"):
            continue
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"], user=user):
            continue
        return True
    return False


def _parse_pin(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    try:
        d = json.loads(raw)
        return (d.get("provider") or "").strip() or None, (d.get("model_key") or "").strip() or None
    except Exception:
        return None, None


def _get_pin(job_cache_key: str | None) -> tuple[str | None, str | None]:
    if not job_cache_key:
        return None, None
    try:
        r = _redis_client()
        raw = r.get(PIN_PREFIX + str(job_cache_key))
        return _parse_pin(raw)
    except Exception as e:
        logger.debug("pin get failed: %s", e)
        return None, None


def _set_pin(job_cache_key: str, provider: str, model_key: str) -> None:
    try:
        r = _redis_client()
        r.setex(
            PIN_PREFIX + str(job_cache_key),
            PIN_TTL_SECONDS,
            json.dumps({"provider": provider, "model_key": model_key}),
        )
    except Exception as e:
        logger.warning("pin set failed: %s", e)


def _clear_pin(job_cache_key: str | None) -> None:
    if not job_cache_key:
        return
    try:
        r = _redis_client()
        r.delete(PIN_PREFIX + str(job_cache_key))
    except Exception as e:
        logger.debug("pin clear failed: %s", e)


def _tier_pick_index(job_cache_key: str | None, priority: int, pref_ids: list[int], n: int) -> int:
    if n <= 0:
        return 0
    if job_cache_key:
        parts = [str(job_cache_key)] + [str(i) for i in sorted(pref_ids)]
        h = hashlib.sha256("|".join(parts).encode()).hexdigest()
        return int(h, 16) % n
    try:
        r = _redis_client()
        k = f"{RR_PREFIX}{priority}"
        v = r.incr(k)
        r.expire(k, 86400 * 7)
        return (int(v) - 1) % n
    except Exception:
        return 0


def _ordered_eligible_candidates(
    user,
    job_cache_key: str | None,
    prefer_local: bool = True,
    only_local: bool = False,
    allow_local: bool = True,
) -> list[dict]:
    raw = _preference_candidates(user)
    eligible = []
    for c in raw:
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"], user=user):
            continue
        if only_local and not c.get("is_local"):
            continue
        if not allow_local and c.get("is_local"):
            continue
        eligible.append(c)
    if not eligible:
        return []

    pin_p, pin_m = _get_pin(job_cache_key)
    pinned = None
    if pin_p and pin_m is not None:
        for c in eligible:
            if c["provider"] == pin_p and c["model_key"] == pin_m:
                # We already checked cooldown when building eligible list
                pinned = c
                break

    def _sort_and_rotate(candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        min_p = min(c["priority"] for c in candidates)
        tier = [c for c in candidates if c["priority"] == min_p]
        pref_ids = [c["preference_id"] for c in tier]
        idx = _tier_pick_index(job_cache_key, min_p, pref_ids, len(tier))
        tier_rot = tier[idx:] + tier[:idx]
        rest = [c for c in candidates if c["priority"] != min_p]
        rest.sort(key=lambda x: (x["priority"], x["preference_id"]))
        return list(tier_rot) + rest

    locals_in = [c for c in eligible if c.get("is_local")]
    remotes_in = [c for c in eligible if not c.get("is_local")]
    if prefer_local:
        # Local first, then remote failover.
        ordered = _sort_and_rotate(locals_in) + _sort_and_rotate(remotes_in)
    else:
        # Remote/cloud first whenever available. prefer_local=False used to leave
        # preference priority alone, so a top-ranked Ollama row still won heavy
        # workloads (e.g. optimizer Writer). Demote local unless no remotes exist.
        ordered = _sort_and_rotate(remotes_in) + _sort_and_rotate(locals_in)

    if pinned is not None and pinned in ordered:
        # Do not let a prior local pin override remote-first routing when remotes exist.
        if (
            not prefer_local
            and pinned.get("is_local")
            and remotes_in
        ):
            return ordered
        # Never promote a local pin when local is disallowed.
        if not allow_local and pinned.get("is_local"):
            return ordered
        ordered.remove(pinned)
        ordered.insert(0, pinned)
    return ordered


def _build_llm_callable(cand: dict):
    return get_llm(cand["provider"], cand["api_key"], cand["model_get_llm"])


def _finalize_usage(
    reconcile,
    raw,
    structured_schema,
    config,
    est,
    _normalize_token_usage,
    provider,
    model_gl,
    *,
    query_kind: str | None = None,
    user=None,
) -> None:
    in_tok, out_tok, cached_tok, estimated = est, 0, 0, True
    reconcile_val = est
    if structured_schema is None and raw is not None:
        u = _normalize_token_usage(raw, getattr(raw, "llm_output", None), None)
        in_tok = int(u["input_tokens"] or 0)
        out_tok = int(u["output_tokens"] or 0)
        cached_tok = int(u.get("cached_tokens") or 0)
        estimated = bool(u.get("tokens_estimated"))
        reconcile_val = in_tok or est
    elif config and isinstance(config, dict):
        for cb in config.get("callbacks") or []:
            if hasattr(cb, "total_input_tokens"):
                tin, tout = cb.total_input_tokens, cb.total_output_tokens
                if tin or tout:
                    in_tok, out_tok = int(tin), int(tout)
                    cached_tok = int(getattr(cb, "total_cached_prompt_tokens", 0) or 0)
                    estimated = False
                    reconcile_val = in_tok or est
                    break
    reconcile(reconcile_val)
    record_llm_usage(
        provider,
        model_gl,
        in_tok,
        out_tok,
        cached_tok,
        estimated,
        query_kind=query_kind,
        user=user,
    )
    try:
        consume_token_budget(user, int(in_tok) + int(out_tok), provider=provider)
    except Exception as ex:
        logger.debug("token budget consume skipped: %s", ex)


def invoke_llm_messages(
    messages,
    *,
    user,
    job_cache_key: str | None = None,
    structured_schema=None,
    config: dict | None = None,
    llm_override: Any | None = None,
    max_attempts_per_model: int = 2,
    usage_query_kind: str | None = None,
    prefer_local: bool = True,
    only_local: bool = False,
    allow_local: bool = True,
) -> Any:
    """
    Invoke LangChain chat messages through the central gateway.

    When llm_override is set, selection and pinning are skipped; rate limits still apply via acquire_llm_slot.
    When allow_local=False, local providers (including Ollama Local) are excluded; if none remain,
    raises LLMUnavailableError.
    """
    from .agents import _normalize_token_usage
    from .llm_policy import assert_llm_kill_switch
    from .rate_limits import check_user_llm_rate_limit, record_llm_request

    check_user_llm_rate_limit(user)
    assert_llm_kill_switch(user)

    est = estimate_tokens_from_messages(messages)

    if llm_override is None and job_cache_key and job_cache_key.strip().isdigit() and usage_query_kind:
        try:
            from .models import OptimizedResume, LLMProviderConfig
            from .crypto import decrypt_api_key
            from .llm_factory import get_llm

            opt = OptimizedResume.objects.select_related("optimizer_workflow").get(id=int(job_cache_key))
            wf = opt.optimizer_workflow
            if wf and wf.step_llm_config:
                # Map usage_query_kind to workflow step type keys ('writer', 'jd_cleanse', etc.)
                step_key_map = {
                    "jd_cleanse": "jd_cleanse",
                    "optimizer_writer": "writer",
                    "optimizer_ats_judge": "ats_judge",
                    "optimizer_recruiter_judge": "recruiter_judge",
                }
                step_key = step_key_map.get(usage_query_kind)
                if step_key and step_key in wf.step_llm_config:
                    cfg = wf.step_llm_config[step_key]
                    prov = (cfg.get("provider") or "").strip()
                    model_to_use = (cfg.get("model") or "").strip() or None
                    if prov:
                        provider_config = LLMProviderConfig.objects.for_user(user).filter(provider=prov).first()
                        if provider_config:
                            api_key = decrypt_api_key(provider_config.encrypted_api_key or "")
                            if api_key:
                                custom_llm = get_llm(prov, api_key, model_to_use)
                                if custom_llm is not None:
                                    logger.info(
                                        "[llm_gateway] Workflow step LLM override applied: step_key=%s -> provider=%s, model=%s",
                                        step_key, prov, model_to_use
                                    )
                                    llm_override = custom_llm
        except Exception as e:
            logger.warning("Failed to apply workflow step LLM override: %s", e)

    if llm_override is not None:
        provider_hint = getattr(llm_override, "_resume_provider", None) or ""
        if not allow_local and provider_is_local(provider_hint):
            raise LLMUnavailableError(NO_CLOUD_LLM_MESSAGE)
        check_token_budget(user, estimated_tokens=est, provider=provider_hint)
        record_llm_request(user)
        with user_llm_concurrency(user):
            return _invoke_single_llm(
                llm_override,
                messages,
                structured_schema=structured_schema,
                config=config,
                _normalize_token_usage=_normalize_token_usage,
                job_cache_key=job_cache_key,
                usage_query_kind=usage_query_kind,
                user=user,
                via="gateway-override",
            )

    tenant_label = getattr(user, "username", getattr(user, "id", "anonymous")) if user else "anonymous"
    candidates = _ordered_eligible_candidates(
        user,
        job_cache_key,
        prefer_local=prefer_local,
        only_local=only_local,
        allow_local=allow_local,
    )
    if not candidates:
        if not allow_local:
            raise LLMUnavailableError(f"[tenant={tenant_label}] {NO_CLOUD_LLM_MESSAGE}")
        raise RuntimeError(
            f"[tenant={tenant_label}] No eligible LLM candidates (check provider keys, preferences, and cooldowns)."
        )

    logger.warning(
        "[llm] tenant=%s candidate order for query=%s prefer_local=%s only_local=%s allow_local=%s: %s",
        tenant_label,
        usage_query_kind or USAGE_QUERY_UNSPECIFIED,
        prefer_local,
        only_local,
        allow_local,
        ", ".join(
            f"{c['provider']}/{c.get('model_get_llm') or c.get('model_key')}"
            + (" (local)" if c.get("is_local") else "")
            for c in candidates[:5]
        )
        + (" ..." if len(candidates) > 5 else ""),
    )

    # Budget check against the first candidate's billing scope (BYOK vs platform).
    check_token_budget(user, estimated_tokens=est, provider=candidates[0]["provider"])
    record_llm_request(user)
    last_exc: Exception | None = None

    with user_llm_concurrency(user):
        for cand in candidates:
            provider = cand["provider"]
            model_gl = cand["model_get_llm"]
            mkey = cand["model_key"]
            reconcile, release = try_acquire_llm_slot(
                provider, model_gl, est, user=user, prefer_failover=True
            )
            if reconcile is None:
                logger.warning(
                    "Skipping %s/%s: rate limit bucket full (try next candidate)",
                    provider,
                    model_gl,
                )
                cd_s = get_cooldown_seconds_for_provider_model(provider, model_gl, user=user)
                set_llm_cooldown(provider, model_gl, cd_s, user=user)
                _clear_pin(job_cache_key)
                continue

            log_llm_invoke(
                provider,
                model_gl or mkey,
                query=usage_query_kind,
                via="gateway",
                extra="local" if cand.get("is_local") else "",
            )
            llm = _build_llm_callable(cand)
            invoke_llm = (
                llm.with_structured_output(structured_schema)
                if structured_schema is not None
                else llm
            )
            for attempt in range(max_attempts_per_model):
                try:

                    def _do_invoke(_invoke_llm=invoke_llm, _config=config):
                        if _config is not None:
                            return _invoke_llm.invoke(messages, config=_config)
                        return _invoke_llm.invoke(messages)

                    raw = run_with_invoke_timeout(_do_invoke)
                except LLMInvokeTimeout:
                    release()
                    raise
                except Exception as e:
                    release()
                    last_exc = e
                    if _is_rate_limit_error(e):
                        cd_s = get_cooldown_seconds_for_provider_model(
                            provider, model_gl, user=user
                        )
                        set_llm_cooldown(provider, model_gl, cd_s, user=user)
                        _clear_pin(job_cache_key)
                        logger.warning(
                            "429/quota on %s/%s; cooldown %ss",
                            provider,
                            model_gl,
                            cd_s,
                        )
                        break
                    raise
                else:
                    try:
                        _finalize_usage(
                            reconcile,
                            raw,
                            structured_schema,
                            config,
                            est,
                            _normalize_token_usage,
                            provider,
                            model_gl,
                            query_kind=usage_query_kind,
                            user=user,
                        )
                    except Exception as ex:
                        logger.debug("token reconcile/record skipped: %s", ex)
                    if job_cache_key:
                        _set_pin(job_cache_key, provider, mkey)
                    return raw
            continue

    if last_exc:
        raise last_exc
    if not allow_local:
        raise LLMUnavailableError(NO_CLOUD_LLM_MESSAGE)
    raise RuntimeError("All LLM candidates exhausted.")


def call_invoke_llm_messages(messages, /, **kwargs) -> Any:
    """
    Call invoke_llm_messages, omitting keyword args the loaded implementation does not accept.
    Avoids TypeError when a Huey/runner process still has an older gateway signature (e.g. missing usage_query_kind).
    """
    sig = inspect.signature(invoke_llm_messages)
    allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return invoke_llm_messages(messages, **allowed)


def _invoke_single_llm(
    llm,
    messages,
    *,
    structured_schema=None,
    config=None,
    _normalize_token_usage,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
    user=None,
    via: str = "gateway-override",
) -> Any:
    provider = getattr(llm, "_resume_provider", None) or "unknown"
    model_gl = getattr(llm, "_resume_model", None)
    log_llm_invoke(provider, model_gl, query=usage_query_kind, via=via)
    est = estimate_tokens_from_messages(messages)
    reconcile, release = acquire_llm_slot(provider, model_gl, est, user=user)
    invoke_llm = (
        llm.with_structured_output(structured_schema) if structured_schema is not None else llm
    )
    try:

        def _do_invoke():
            if config is not None:
                return invoke_llm.invoke(messages, config=config)
            return invoke_llm.invoke(messages)

        raw = run_with_invoke_timeout(_do_invoke)
    except Exception as e:
        release()
        if _is_rate_limit_error(e):
            set_llm_cooldown(
                provider,
                model_gl,
                get_cooldown_seconds_for_provider_model(provider, model_gl, user=user),
                user=user,
            )
            _clear_pin(job_cache_key)
        raise
    try:
        _finalize_usage(
            reconcile,
            raw,
            structured_schema,
            config,
            est,
            _normalize_token_usage,
            provider,
            model_gl,
            query_kind=usage_query_kind,
            user=user,
        )
    except Exception as ex:
        logger.debug("token reconcile/record skipped: %s", ex)
    return raw


def invoke_llm_messages_with_retry(
    messages,
    *,
    user,
    job_cache_key: str | None = None,
    structured_schema=None,
    config=None,
    llm_override: Any | None = None,
    max_attempts_per_model: int = 2,
    usage_query_kind: str | None = None,
) -> Any:
    """Same as invoke_llm_messages; retry wrapper reserved for future use."""
    return call_invoke_llm_messages(
        messages,
        user=user,
        job_cache_key=job_cache_key,
        structured_schema=structured_schema,
        config=config,
        llm_override=llm_override,
        max_attempts_per_model=max_attempts_per_model,
        usage_query_kind=usage_query_kind,
    )
