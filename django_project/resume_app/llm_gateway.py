"""
Central LLM invoke path: kill switch, preference order, job pinning, cooldowns, usage stats.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from django.db.models import F
from django.utils import timezone

from .crypto import decrypt_api_key
from .llm_factory import get_llm
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

# Keys for `usage_query_kind` / `LLMUsageByQuery.query_kind` (Settings → Usage labels in USAGE_QUERY_LABELS).
USAGE_QUERY_FIT_CHECK = "fit_check"
USAGE_QUERY_MATCHING = "matching"
USAGE_QUERY_OPTIMIZER_WRITER = "optimizer_writer"
USAGE_QUERY_OPTIMIZER_ATS_JUDGE = "optimizer_ats_judge"
USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE = "optimizer_recruiter_judge"
USAGE_QUERY_JOB_INSIGHTS = "job_insights"
USAGE_QUERY_JOBS_AI_MATCH = "jobs_ai_match"
USAGE_QUERY_KEYWORD_SEARCH_FIT = "keyword_search_fit"
USAGE_QUERY_JOBS_MATCH_API = "jobs_match_api"
USAGE_QUERY_PIPELINE_VETTING = "pipeline_vetting_matching"
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
    USAGE_QUERY_JOBS_AI_MATCH: "Job search — AI match",
    USAGE_QUERY_KEYWORD_SEARCH_FIT: "Job search — keyword search fit",
    USAGE_QUERY_JOBS_MATCH_API: "Job search — single job fit (API)",
    USAGE_QUERY_PIPELINE_VETTING: "Pipeline — vetting match",
    USAGE_QUERY_API_LLM_COMPLETE: "HTTP API — LLM complete",
    USAGE_QUERY_API_RESUME_FIT: "HTTP API — resume fit (multipart)",
}

PIN_PREFIX = "llm:pin:v1:"
RR_PREFIX = "llm:rr:v1:"
PIN_TTL_SECONDS = 86400 * 2


class LLMRequestsDisabled(Exception):
    """Raised when AppAutomationSettings.stop_llm_requests is True."""


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
    query_kind: str | None = None,
) -> None:
    prov, mkey = normalize_usage_model_key(provider, model)
    try:
        solo = LLMAppUsageTotals.get_solo()
        LLMAppUsageTotals.objects.filter(pk=solo.pk).update(
            total_input_tokens=F("total_input_tokens") + max(0, int(input_tokens)),
            total_output_tokens=F("total_output_tokens") + max(0, int(output_tokens)),
            total_requests=F("total_requests") + 1,
            total_estimated_invokes=F("total_estimated_invokes") + (1 if tokens_estimated else 0),
        )
    except Exception as e:
        logger.warning("record_llm_usage totals failed: %s", e)
    try:
        row, _created = LLMUsageByModel.objects.get_or_create(
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


def _preference_candidates() -> list[dict]:
    rows = (
        LLMProviderPreference.objects.select_related("provider_config")
        .filter(
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
                "preference_id": row.id,
                "config": cfg,
                "api_key": api_key,
            }
        )
    return out


def preference_candidates_available() -> bool:
    return bool(_preference_candidates())


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
    job_cache_key: str | None,
) -> list[dict]:
    raw = _preference_candidates()
    eligible = []
    for c in raw:
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"]):
            continue
        eligible.append(c)
    if not eligible:
        return []

    pin_p, pin_m = _get_pin(job_cache_key)
    pinned = None
    if pin_p and pin_m is not None:
        for c in eligible:
            if c["provider"] == pin_p and c["model_key"] == pin_m and not is_llm_on_cooldown(
                c["provider"], c["model_get_llm"]
            ):
                pinned = c
                break

    min_p = min(c["priority"] for c in eligible)
    tier = [c for c in eligible if c["priority"] == min_p]
    pref_ids = [c["preference_id"] for c in tier]
    idx = _tier_pick_index(job_cache_key, min_p, pref_ids, len(tier))
    tier_rot = tier[idx:] + tier[:idx]

    rest = [c for c in eligible if c["priority"] != min_p]
    rest.sort(key=lambda x: (x["priority"], x["preference_id"]))
    ordered = list(tier_rot) + rest
    if pinned is not None and pinned in ordered:
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
    )


def invoke_llm_messages(
    messages,
    *,
    job_cache_key: str | None = None,
    structured_schema=None,
    config: dict | None = None,
    llm_override: Any | None = None,
    max_attempts_per_model: int = 2,
    usage_query_kind: str | None = None,
) -> Any:
    """
    Invoke LangChain chat messages through the central gateway.

    When llm_override is set, selection and pinning are skipped; rate limits still apply via acquire_llm_slot.
    """
    from .agents import _normalize_token_usage
    from .models import AppAutomationSettings

    if AppAutomationSettings.get_solo().stop_llm_requests:
        raise LLMRequestsDisabled("LLM requests are disabled. Turn off 'Stop LLM requests' in Settings → LLM.")

    if llm_override is not None:
        return _invoke_single_llm(
            llm_override,
            messages,
            structured_schema=structured_schema,
            config=config,
            _normalize_token_usage=_normalize_token_usage,
            job_cache_key=job_cache_key,
            usage_query_kind=usage_query_kind,
        )

    candidates = _ordered_eligible_candidates(job_cache_key)
    if not candidates:
        raise RuntimeError(
            "No eligible LLM candidates (check provider keys, preferences, and cooldowns)."
        )

    est = estimate_tokens_from_messages(messages)
    last_exc: Exception | None = None

    for cand in candidates:
        provider = cand["provider"]
        model_gl = cand["model_get_llm"]
        mkey = cand["model_key"]
        reconcile, release = try_acquire_llm_slot(
            provider, model_gl, est, prefer_failover=True
        )
        if reconcile is None:
            logger.warning(
                "Skipping %s/%s: rate limit bucket full (try next candidate)",
                provider,
                model_gl,
            )
            cd_s = get_cooldown_seconds_for_provider_model(provider, model_gl)
            set_llm_cooldown(provider, model_gl, cd_s)
            _clear_pin(job_cache_key)
            continue

        llm = _build_llm_callable(cand)
        invoke_llm = (
            llm.with_structured_output(structured_schema)
            if structured_schema is not None
            else llm
        )
        for attempt in range(max_attempts_per_model):
            try:
                if config is not None:
                    raw = invoke_llm.invoke(messages, config=config)
                else:
                    raw = invoke_llm.invoke(messages)
            except Exception as e:
                release()
                last_exc = e
                if _is_rate_limit_error(e):
                    cd_s = get_cooldown_seconds_for_provider_model(provider, model_gl)
                    set_llm_cooldown(provider, model_gl, cd_s)
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
                    )
                except Exception as ex:
                    logger.debug("token reconcile/record skipped: %s", ex)
                if job_cache_key:
                    _set_pin(job_cache_key, provider, mkey)
                return raw
        continue

    if last_exc:
        raise last_exc
    raise RuntimeError("All LLM candidates exhausted.")


def _invoke_single_llm(
    llm,
    messages,
    *,
    structured_schema=None,
    config=None,
    _normalize_token_usage,
    job_cache_key: str | None = None,
    usage_query_kind: str | None = None,
) -> Any:
    provider = getattr(llm, "_resume_provider", None) or "unknown"
    model_gl = getattr(llm, "_resume_model", None)
    est = estimate_tokens_from_messages(messages)
    reconcile, release = acquire_llm_slot(provider, model_gl, est)
    invoke_llm = (
        llm.with_structured_output(structured_schema) if structured_schema is not None else llm
    )
    try:
        if config is not None:
            raw = invoke_llm.invoke(messages, config=config)
        else:
            raw = invoke_llm.invoke(messages)
    except Exception as e:
        release()
        if _is_rate_limit_error(e):
            set_llm_cooldown(
                provider,
                model_gl,
                get_cooldown_seconds_for_provider_model(provider, model_gl),
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
        )
    except Exception as ex:
        logger.debug("token reconcile/record skipped: %s", ex)
    return raw


def invoke_llm_messages_with_retry(
    messages,
    *,
    job_cache_key: str | None = None,
    structured_schema=None,
    config=None,
    llm_override: Any | None = None,
    max_attempts_per_model: int = 2,
    usage_query_kind: str | None = None,
) -> Any:
    """Same as invoke_llm_messages; retry wrapper reserved for future use."""
    return invoke_llm_messages(
        messages,
        job_cache_key=job_cache_key,
        structured_schema=structured_schema,
        config=config,
        llm_override=llm_override,
        max_attempts_per_model=max_attempts_per_model,
        usage_query_kind=usage_query_kind,
    )
