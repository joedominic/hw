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

from ..crypto import decrypt_api_key
from .factory import get_llm
from .policy import (
    LLMConcurrencyLimitExceeded,
    LLMEmailVerificationRequired,
    LLMInvokeTimeout,
    LLMRequestsDisabled,
    LLMTokenBudgetExceeded,
    assert_email_verified,
    check_token_budget,
    consume_token_budget,
    run_with_invoke_timeout,
    user_llm_concurrency,
)
from .rate_limit import (
    acquire_llm_slot,
    estimate_tokens_from_messages,
    get_cooldown_seconds_for_provider_model,
    is_llm_on_cooldown,
    set_llm_cooldown,
    try_acquire_llm_slot,
)
from ..models import (
    LLMProviderPreference,
    LLMAppUsageTotals,
    LLMUsageByModel,
    LLMUsageByQuery,
    LLMDailyUsageBreakdown,
)

logger = logging.getLogger(__name__)

# Re-export for callers that import kill-switch from the gateway.
__all__ = [
    "LLMRequestsDisabled",
    "LLMEmailVerificationRequired",
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
    "MODEL_TIER_SLM",
    "MODEL_TIER_FRONTIER",
    "classify_model_tier",
]

MODEL_TIER_SLM = "slm"
MODEL_TIER_FRONTIER = "frontier"


def classify_model_tier(provider: str, model: str | None) -> str:
    """Classifies an LLM candidate as SLM (small/flash/local) or Frontier (large reasoning/writer).

    Guarantees background evaluation & vetting runs never fall back to expensive Frontier models
    (e.g., GPT-4o, Claude 3.5 Sonnet) and burn tokens/budget.
    """
    prov = (provider or "").strip()
    m = (model or "").strip().lower()

    if provider_is_local(prov):
        return MODEL_TIER_SLM

    if prov == "Google AI Studio":
        if "flash" in m or "lite" in m or "8b" in m:
            return MODEL_TIER_SLM
        return MODEL_TIER_FRONTIER

    if prov == "OpenAI":
        if "mini" in m or "turbo" in m or "nano" in m:
            return MODEL_TIER_SLM
        return MODEL_TIER_FRONTIER

    if prov == "Anthropic":
        if "haiku" in m:
            return MODEL_TIER_SLM
        return MODEL_TIER_FRONTIER

    if prov == "Groq":
        if any(x in m for x in ("8b", "7b", "9b", "instant", "small", "mini")):
            return MODEL_TIER_SLM
        return MODEL_TIER_FRONTIER

    if prov == "OpenRouter":
        if any(x in m for x in ("mini", "flash", "haiku", "8b", "7b", "3b", "nano", "lite")):
            return MODEL_TIER_SLM
        return MODEL_TIER_FRONTIER

    if any(x in m for x in ("mini", "flash", "haiku", "nano", "small", "8b", "7b", "3b", "lite")):
        return MODEL_TIER_SLM

    return MODEL_TIER_FRONTIER

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
USAGE_QUERY_JOB_AUTHENTICITY = "job_authenticity"
USAGE_QUERY_SKILL_RADAR = "skill_radar"
USAGE_QUERY_RESUME_JUDGE = "resume_judge"
USAGE_QUERY_CAREER_PROFILER = "career_profiler"
USAGE_QUERY_PROMPT_STUDIO = "prompt_studio"
USAGE_QUERY_API_LLM_COMPLETE = "api_llm_complete"
USAGE_QUERY_API_RESUME_FIT = "api_resume_fit"
USAGE_QUERY_UNSPECIFIED = "unspecified"

USAGE_QUERY_LABELS: dict[str, str] = {
    USAGE_QUERY_UNSPECIFIED: "Other / not labeled",
    USAGE_QUERY_PROMPT_STUDIO: "Prompt Studio — test playground",
    USAGE_QUERY_CAREER_PROFILER: "Career profiler — archetype mapping",
    USAGE_QUERY_SKILL_RADAR: "Pipeline — skill radar & deep fit",
    USAGE_QUERY_FIT_CHECK: "Fit check",
    USAGE_QUERY_MATCHING: "Job matching",
    USAGE_QUERY_OPTIMIZER_WRITER: "Resume optimizer — writer",
    USAGE_QUERY_RESUME_JUDGE: "Resume Evaluator — Resume Judge (ATS + Recruiter)",
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
    USAGE_QUERY_API_LLM_COMPLETE: "HTTP API — LLM complete",
    USAGE_QUERY_API_RESUME_FIT: "HTTP API — resume fit (multipart)",
}

# System default fallback models when a tenant has no BYOK keys configured
# or when all tenant candidate preferences are on cooldown.
SYSTEM_DEFAULT_MODELS: dict[str, list[dict]] = {
    USAGE_QUERY_CAREER_PROFILER: [
        {"provider": "Google AI Studio", "model": "gemini-2.0-flash"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    USAGE_QUERY_PROMPT_STUDIO: [
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "Google AI Studio", "model": "gemini-2.0-flash"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    # 1. Parsing & Cleansing
    USAGE_QUERY_JD_CLEANSE: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
    ],
    USAGE_QUERY_PIPELINE_SKILL_EXTRACT: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
    ],
    # 2. Search & Vetting
    USAGE_QUERY_SKILL_RADAR: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "Google AI Studio", "model": "gemini-2.0-flash"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Groq", "model": "llama-3.1-8b-instant"},
        # Frontier Cloud models for deep on-demand evaluations
        {"provider": "OpenAI", "model": "gpt-4o"},
        {"provider": "Anthropic", "model": "claude-3-5-sonnet-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-pro"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
    ],
    USAGE_QUERY_MATCHING: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
    ],
    USAGE_QUERY_FIT_CHECK: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
    ],
    USAGE_QUERY_PIPELINE_VETTING: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
    ],
    USAGE_QUERY_JOBS_AI_MATCH: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    USAGE_QUERY_KEYWORD_SEARCH_FIT: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    USAGE_QUERY_JOBS_MATCH_API: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    USAGE_QUERY_JOB_INSIGHTS: [
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    # 3. Resume Writer
    USAGE_QUERY_OPTIMIZER_WRITER: [
        {"provider": "OpenAI", "model": "gpt-4o"},
        {"provider": "Anthropic", "model": "claude-3-5-sonnet-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-pro"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
    ],
    # 4. Judges & Evaluators
    USAGE_QUERY_RESUME_JUDGE: [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
    ],
    USAGE_QUERY_OPTIMIZER_ATS_JUDGE: [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE: [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    # 5. Career Prep
    USAGE_QUERY_COVER_LETTER: [
        {"provider": "Anthropic", "model": "claude-3-5-sonnet-latest"},
        {"provider": "OpenAI", "model": "gpt-4o"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "Google AI Studio", "model": "gemini-2.0-flash"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    USAGE_QUERY_INTERVIEW_PREP: [
        {"provider": "Anthropic", "model": "claude-3-5-sonnet-latest"},
        {"provider": "OpenAI", "model": "gpt-4o"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
        {"provider": "Google AI Studio", "model": "gemini-2.0-flash"},
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
    ],
    # 6. APIs
    USAGE_QUERY_API_LLM_COMPLETE: [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    USAGE_QUERY_API_RESUME_FIT: [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
    ],
    # Universal fallback
    "*": [
        {"provider": "OpenAI", "model": "gpt-4o-mini"},
        {"provider": "Anthropic", "model": "claude-3-5-haiku-latest"},
        {"provider": "Google AI Studio", "model": "gemini-1.5-flash"},
        {"provider": "Ollama Local", "model": "hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M"},
        {"provider": "Groq", "model": "llama-3.3-70b-versatile"},
    ],
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
    from .rate_limit import _get_redis

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
    try:
        today_date = timezone.localdate()
        drow, _ = LLMDailyUsageBreakdown.objects.get_or_create(
            owner=user,
            period_date=today_date,
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
        LLMDailyUsageBreakdown.objects.filter(pk=drow.pk).update(
            request_count=F("request_count") + 1,
            sum_input_tokens=F("sum_input_tokens") + max(0, int(input_tokens)),
            sum_output_tokens=F("sum_output_tokens") + max(0, int(output_tokens)),
            sum_cached_tokens=F("sum_cached_tokens") + max(0, int(cached_tokens)),
            last_used_at=timezone.now(),
        )
    except Exception as e:
        logger.warning("record_llm_usage daily breakdown failed: %s", e)


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
                "model_tier": getattr(row, "model_tier", None) or classify_model_tier(prov, raw_model),
            }
        )
    return out


def preference_candidates_available(user) -> bool:
    return bool(_preference_candidates(user))


def platform_key_available_for_provider(provider: str) -> bool:
    """Check if environment/settings has a usable key for this provider."""
    from django.conf import settings

    prov = (provider or "").strip()
    if prov == "OpenAI":
        return bool(getattr(settings, "OPENAI_API_KEY", None))
    elif prov == "Anthropic":
        return bool(getattr(settings, "ANTHROPIC_API_KEY", None))
    elif prov == "Groq":
        return bool(getattr(settings, "GROQ_API_KEY", None))
    elif prov == "Google AI Studio":
        return bool(getattr(settings, "GOOGLE_API_KEY", None))
    elif prov == "Ollama Cloud":
        return bool(getattr(settings, "OLLAMA_API_KEY", None))
    elif prov == "Ollama Local":
        return bool(
            getattr(settings, "OLLAMA_LOCAL_HOST", None)
            or getattr(settings, "OLLAMA_HOST", None)
            or True
        )
    elif prov == "OpenRouter":
        return bool(getattr(settings, "OPENROUTER_API_KEY", None))
    return False


def _system_default_candidates(
    usage_query_kind: str | None = None,
    user=None,
    only_local: bool = False,
    allow_local: bool = True,
    prefer_local: bool = True,
    max_model_tier: str | None = None,
) -> list[dict]:
    """
    Build platform system default candidate list for the given query kind.
    Filters by available platform environment credentials and cooldown status.
    """
    qk = (usage_query_kind or "").strip() or "*"
    specs = SYSTEM_DEFAULT_MODELS.get(qk) or SYSTEM_DEFAULT_MODELS.get("*") or []
    out = []
    seen = set()
    for spec in specs:
        prov = spec["provider"]
        model = spec["model"]
        key = (prov, model)
        if key in seen:
            continue
        seen.add(key)

        is_loc = provider_is_local(prov)
        if only_local and not is_loc:
            continue
        if not allow_local and is_loc:
            continue
        if not platform_key_available_for_provider(prov):
            continue
        if is_llm_on_cooldown(prov, model, user=user):
            continue
        spec_tier = classify_model_tier(prov, model)
        if max_model_tier == MODEL_TIER_SLM and spec_tier != MODEL_TIER_SLM:
            continue
        if max_model_tier == MODEL_TIER_FRONTIER and spec_tier != MODEL_TIER_FRONTIER:
            continue

        cand_key = None
        cand_cfg = None
        if prov == "Ollama Local":
            if user:
                try:
                    from ..models import LLMProviderConfig
                    from ..crypto import decrypt_api_key

                    cand_cfg = (
                        LLMProviderConfig.objects.for_user(user)
                        .filter(provider="Ollama Local")
                        .first()
                    )
                    if cand_cfg and cand_cfg.encrypted_api_key:
                        cand_key = decrypt_api_key(cand_cfg.encrypted_api_key)
                except Exception:
                    pass
            if not cand_key:
                from django.conf import settings

                cand_key = (
                    getattr(settings, "OLLAMA_LOCAL_HOST", None)
                    or getattr(settings, "OLLAMA_HOST", None)
                    or "http://127.0.0.1:11434"
                )

        out.append(
            {
                "provider": prov,
                "model_get_llm": model,
                "model_key": model or "__default__",
                "priority": 999,
                "is_local": is_loc,
                "preference_id": None,
                "config": cand_cfg,
                "api_key": cand_key,
                "user": user,
                "is_system_default": True,
            }
        )

    locals_in = [c for c in out if c.get("is_local")]
    remotes_in = [c for c in out if not c.get("is_local")]
    if prefer_local:
        return locals_in + remotes_in
    return remotes_in + locals_in


def cloud_llm_available(user) -> bool:
    """True when at least one non-local candidate (BYOK or platform default) is configured and not on cooldown."""
    for c in _preference_candidates(user):
        if c.get("is_local"):
            continue
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"], user=user):
            continue
        return True
    for c in _system_default_candidates(user=user, only_local=False, allow_local=False):
        if not c.get("is_local"):
            return True
    return False


def local_llm_available(user) -> bool:
    """True when at least one local candidate (BYOK or platform Ollama) is configured and not on cooldown."""
    for c in _preference_candidates(user):
        if not c.get("is_local"):
            continue
        if is_llm_on_cooldown(c["provider"], c["model_get_llm"], user=user):
            continue
        return True
    for c in _system_default_candidates(user=user, only_local=True, allow_local=True):
        if c.get("is_local"):
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
    usage_query_kind: str | None = None,
    prefer_local: bool = True,
    only_local: bool = False,
    allow_local: bool = True,
    max_model_tier: str | None = None,
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
        cand_tier = c.get("model_tier") or classify_model_tier(c["provider"], c["model_get_llm"])
        if max_model_tier == MODEL_TIER_SLM and cand_tier != MODEL_TIER_SLM:
            continue
        if max_model_tier == MODEL_TIER_FRONTIER and cand_tier != MODEL_TIER_FRONTIER:
            continue
        eligible.append(c)

    def _sort_and_rotate(candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        min_p = min(c["priority"] for c in candidates)
        tier = [c for c in candidates if c["priority"] == min_p]
        pref_ids = [c["preference_id"] for c in tier if c.get("preference_id")]
        idx = _tier_pick_index(job_cache_key, min_p, pref_ids, len(tier))
        tier_rot = tier[idx:] + tier[:idx]
        rest = [c for c in candidates if c["priority"] != min_p]
        rest.sort(key=lambda x: (x["priority"], x.get("preference_id") or 0))
        return list(tier_rot) + rest

    ordered = []
    if eligible:
        locals_in = [c for c in eligible if c.get("is_local")]
        remotes_in = [c for c in eligible if not c.get("is_local")]
        if prefer_local:
            ordered = _sort_and_rotate(locals_in) + _sort_and_rotate(remotes_in)
        else:
            ordered = _sort_and_rotate(remotes_in) + _sort_and_rotate(locals_in)

    # System Platform Default Models Fallback
    # If the tenant has configured their own key/model in this tier, use that;
    # otherwise fall back to system default models in that tier.
    if not ordered:
        ordered = _system_default_candidates(
            usage_query_kind=usage_query_kind,
            user=user,
            only_local=only_local,
            allow_local=allow_local,
            prefer_local=prefer_local,
            max_model_tier=max_model_tier,
        )

    pin_p, pin_m = _get_pin(job_cache_key)
    pinned = None
    if pin_p and pin_m is not None:
        for c in ordered:
            if c["provider"] == pin_p and c["model_key"] == pin_m:
                pinned = c
                break

    if pinned is not None and pinned in ordered:
        if (
            not prefer_local
            and pinned.get("is_local")
            and any(not c.get("is_local") for c in ordered)
        ):
            pass
        elif not allow_local and pinned.get("is_local"):
            pass
        else:
            ordered.remove(pinned)
            ordered.insert(0, pinned)

    return ordered


def _build_llm_callable(cand: dict, metadata: dict | None = None):
    user = cand.get("user") or (getattr(cand.get("config"), "owner", None) if cand.get("config") else None)
    return get_llm(cand["provider"], cand["api_key"], cand["model_get_llm"], user=user, metadata=metadata)


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
    if raw is not None:
        u = _normalize_token_usage(raw, getattr(raw, "llm_output", None), None)
        in_tok = int(u["input_tokens"] or 0) or est
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
        consume_token_budget(
            user,
            int(in_tok) + int(out_tok),
            provider=provider,
            is_local=provider_is_local(provider),
        )
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
    max_model_tier: str | None = None,
) -> Any:
    """
    Invoke LangChain chat messages through the central gateway.

    When llm_override is set, selection and pinning are skipped; rate limits still apply via acquire_llm_slot.
    When allow_local=False, local providers (including Ollama Local) are excluded; if none remain,
    raises LLMUnavailableError.
    """
    from ..agents import _normalize_token_usage
    from .policy import assert_email_verified, assert_llm_kill_switch
    from ..rate_limits import check_user_llm_rate_limit, record_llm_request

    check_user_llm_rate_limit(user)
    assert_llm_kill_switch(user)
    assert_email_verified(user)

    est = estimate_tokens_from_messages(messages)
    # Dynamic prompt routing: driven entirely by TenantPromptModelPreference / UI (no hardcoding in code!)
    if usage_query_kind and max_model_tier is None:
        try:
            from ..models import TenantPromptModelPreference
            from ..prompt_store import get_recommended_tier_for_query_kind

            pref_row = None
            if user is not None and getattr(user, "is_authenticated", False):
                pref_row = TenantPromptModelPreference.objects.for_user(user).filter(
                    query_kind=usage_query_kind, is_active=True
                ).first()

            p_tier = getattr(pref_row, "target_tier", "auto") if pref_row else "auto"
            if p_tier == "auto" or not p_tier:
                p_tier = get_recommended_tier_for_query_kind(usage_query_kind)

            if p_tier == "slm":
                max_model_tier = MODEL_TIER_SLM
                prefer_local = True
            elif p_tier == "frontier":
                max_model_tier = MODEL_TIER_FRONTIER
                prefer_local = False
                allow_local = False
        except Exception as e:
            logger.debug("Dynamic prompt tier resolution fallback: %s", e)

    if llm_override is None and job_cache_key and job_cache_key.strip().isdigit() and usage_query_kind:
        try:
            from ..models import OptimizedResume, LLMProviderConfig
            from ..crypto import decrypt_api_key
            from .factory import get_llm

            opt = OptimizedResume.objects.select_related("optimizer_workflow").get(id=int(job_cache_key))
            wf = opt.optimizer_workflow
            if wf and wf.step_llm_config:
                # Map usage_query_kind to workflow step type keys ('writer', 'jd_cleanse', etc.)
                step_key_map = {
                    "jd_cleanse": "jd_cleanse",
                    "optimizer_writer": "writer",
                    "optimizer_ats_judge": "ats_judge",
                    "optimizer_recruiter_judge": "recruiter_judge",
                    "resume_judge": "resume_judge",
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
        check_token_budget(
            user,
            estimated_tokens=est,
            provider=provider_hint,
            is_local=provider_is_local(provider_hint),
        )
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
        usage_query_kind=usage_query_kind,
        prefer_local=prefer_local,
        only_local=only_local,
        allow_local=allow_local,
        max_model_tier=max_model_tier,
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

    record_llm_request(user)
    last_exc: Exception | None = None

    with user_llm_concurrency(user):
        for cand in candidates:
            provider = cand["provider"]
            model_gl = cand["model_get_llm"]
            mkey = cand["model_key"]
            is_cand_local = bool(cand.get("is_local") or provider_is_local(provider))
            try:
                check_token_budget(
                    user,
                    estimated_tokens=est,
                    provider=provider,
                    is_local=is_cand_local,
                )
            except LLMTokenBudgetExceeded as budget_err:
                logger.warning(
                    "Skipping candidate %s/%s: %s (will try next candidate if available)",
                    provider,
                    model_gl,
                    budget_err,
                )
                last_exc = budget_err
                continue

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
            from .telemetry import resolve_trace_names

            t_name, g_name = resolve_trace_names(
                query_kind=usage_query_kind,
                job_cache_key=job_cache_key,
                default_name=model_gl or mkey,
            )
            call_metadata = {
                "query_kind": usage_query_kind or USAGE_QUERY_UNSPECIFIED,
                "provider": provider,
                "model": model_gl or mkey,
                "trace_name": t_name,
                "generation_name": g_name,
            }
            if job_cache_key:
                call_metadata["job_cache_key"] = str(job_cache_key)
                call_metadata["session_id"] = str(job_cache_key)

            llm = _build_llm_callable(cand, metadata=call_metadata)
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
                except LLMInvokeTimeout as e:
                    release()
                    last_exc = e
                    cd_s = get_cooldown_seconds_for_provider_model(
                        provider, model_gl, user=user
                    )
                    set_llm_cooldown(provider, model_gl, cd_s, user=user)
                    _clear_pin(job_cache_key)
                    logger.warning(
                        "Timeout on %s/%s; cooldown %ss (failing over to next candidate)",
                        provider,
                        model_gl,
                        cd_s,
                    )
                    break
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
                    is_last_cand = (cand == candidates[-1])
                    if not is_last_cand and attempt == max_attempts_per_model - 1:
                        cd_s = get_cooldown_seconds_for_provider_model(
                            provider, model_gl, user=user
                        )
                        set_llm_cooldown(provider, model_gl, cd_s, user=user)
                        _clear_pin(job_cache_key)
                        logger.warning(
                            "Error on %s/%s (%s); cooldown %ss (failing over to next candidate)",
                            provider,
                            model_gl,
                            e,
                            cd_s,
                        )
                        break
                    if attempt == max_attempts_per_model - 1:
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

    from .telemetry import resolve_trace_names

    t_name, g_name = resolve_trace_names(
        query_kind=usage_query_kind,
        job_cache_key=job_cache_key,
        default_name=model_gl,
    )
    if hasattr(llm, "metadata"):
        if getattr(llm, "metadata", None) is None:
            llm.metadata = {}
        llm.metadata.setdefault("trace_name", t_name)
        llm.metadata.setdefault("generation_name", g_name)
        llm.metadata.setdefault("query_kind", usage_query_kind or USAGE_QUERY_UNSPECIFIED)
        if job_cache_key:
            llm.metadata.setdefault("job_cache_key", str(job_cache_key))
            llm.metadata.setdefault("session_id", str(job_cache_key))

    call_config = dict(config or {})
    call_meta = dict(call_config.get("metadata") or {})
    call_meta.setdefault("trace_name", t_name)
    call_meta.setdefault("generation_name", g_name)
    call_meta.setdefault("query_kind", usage_query_kind or USAGE_QUERY_UNSPECIFIED)
    if job_cache_key:
        call_meta.setdefault("job_cache_key", str(job_cache_key))
        call_meta.setdefault("session_id", str(job_cache_key))
    call_config["metadata"] = call_meta

    est = estimate_tokens_from_messages(messages)
    reconcile, release = acquire_llm_slot(provider, model_gl, est, user=user)
    invoke_llm = (
        llm.with_structured_output(structured_schema) if structured_schema is not None else llm
    )
    try:

        def _do_invoke():
            return invoke_llm.invoke(messages, config=call_config)

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
