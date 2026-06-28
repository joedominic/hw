"""
Generic fallback agent for unknown ATS, built on ``browser-use``.

Used only when no deterministic adapter matches. It drives an LLM-orchestrated
browser to fill the form and capture a semantic answer key, then stops. It
NEVER submits autonomously: the generic path always requires human approval,
regardless of the global automation mode.

browser-use is imported lazily and the whole run is bounded by a hard timeout
so a runaway agent cannot occupy a scarce browser worker slot.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from django.conf import settings

from .base import ApplyContext, FillResult

logger = logging.getLogger("huey")

GENERIC_AGENT_TIMEOUT_SECONDS = 300  # 5 minute hard cap
MAX_AGENT_STEPS = 30

_availability_error: str | None = None


def _try_import_browser_use() -> bool:
    """Return True when browser-use imports cleanly in this Python environment."""
    global _availability_error
    try:
        import browser_use  # noqa: F401

        _availability_error = None
        return True
    except ImportError as exc:
        _availability_error = str(exc)
        logger.warning("[apply_agent.generic] browser-use import failed: %s", exc)
        return False


def is_available() -> bool:
    return _try_import_browser_use()


def unavailability_reason() -> str:
    """Human-readable reason ``is_available()`` returned False."""
    if is_available():
        return ""
    return _availability_error or "unknown import error"


def _build_task_instructions(ctx: ApplyContext) -> str:
    answers = {
        "Full name": ctx.full_name,
        "Email": ctx.email,
        "Phone": ctx.phone,
        "Location": ctx.location,
        "LinkedIn": ctx.linkedin_url,
        "Website": ctx.website_url,
        "Work authorization": ctx.work_authorization,
        "Requires sponsorship": "Yes" if ctx.requires_sponsorship else "No",
        "Salary expectation": ctx.salary_expectation,
    }
    answers.update({str(k): str(v) for k, v in (ctx.custom_qa or {}).items()})
    lines = "\n".join(f"- {k}: {v}" for k, v in answers.items() if v not in ("", None))
    return (
        f"Open {ctx.apply_url} and fill in the job application form for "
        f"{ctx.job_title or 'this role'} at {ctx.company_name or 'the company'} using the data below. "
        f"Upload the resume file at {ctx.resume_file_path}. "
        "IMPORTANT: Do NOT click the final submit button. Stop once every visible field is filled "
        "and the form is ready for a human to review and submit.\n\n"
        f"Applicant data:\n{lines}"
    )


def resolve_apply_agent_llm_candidate(user) -> dict:
    """Return provider/model/config for browser-use, honoring Apply Agent LLM settings.

    ``user`` is required to scope LLM provider lookups to the owning tenant.
    """
    from ..llm_services import DEFAULT_MODELS
    from ..llm_session import get_runtime_provider_candidates
    from ..models import AppAutomationSettings, LLMProviderConfig

    solo = AppAutomationSettings.get_for_user(user)
    provider = (solo.apply_agent_llm_provider or "").strip()
    model = (solo.apply_agent_llm_model or "").strip()

    if provider:
        cfg = (
            LLMProviderConfig.objects.for_user(user)
            .filter(provider=provider)
            .exclude(encrypted_api_key="")
            .exclude(encrypted_api_key__isnull=True)
            .first()
        )
        if cfg is None:
            raise ValueError(
                f"Apply Agent LLM provider {provider!r} is not configured. "
                "Add an API key in Settings or clear the dedicated provider."
            )
        effective_model = model or (cfg.default_model or "").strip() or DEFAULT_MODELS.get(provider, "")
        if not effective_model:
            raise ValueError(f"No model selected for Apply Agent LLM provider {provider!r}.")
        return {"provider": provider, "model": effective_model, "config": cfg}

    candidates = get_runtime_provider_candidates(user)
    if not candidates:
        raise ValueError("No LLM provider configured. Add an API key in Settings.")
    cand = candidates[0]
    provider = (cand.get("provider") or "").strip()
    effective_model = (cand.get("model") or DEFAULT_MODELS.get(provider) or "").strip()
    if not effective_model:
        raise ValueError(f"No model available for provider {provider!r}.")
    return {"provider": provider, "model": effective_model, "config": cand["config"]}


def _build_browser_use_llm(user) -> Any:
    """Build a browser-use ``BaseChatModel`` for generic form fill."""
    from ..crypto import decrypt_api_key
    from ..llm_factory import _normalize_ollama_local_host

    cand = resolve_apply_agent_llm_candidate(user)
    provider = cand["provider"]
    model = cand["model"]
    cfg = cand["config"]
    secret = decrypt_api_key(cfg.encrypted_api_key or "")
    if not (secret or "").strip():
        raise ValueError(f"No API key configured for {provider}.")

    logger.info("[apply_agent.generic] using LLM provider=%s model=%s", provider, model)

    if provider == "OpenAI":
        from browser_use.llm.openai.chat import ChatOpenAI

        return ChatOpenAI(model=model, api_key=secret)
    if provider == "Anthropic":
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(model=model, api_key=secret)
    if provider == "Groq":
        from browser_use.llm.groq.chat import ChatGroq

        return ChatGroq(model=model, api_key=secret)
    if provider == "Google AI Studio":
        from browser_use.llm.google.chat import ChatGoogle

        return ChatGoogle(model=model, api_key=secret)
    if provider == "Ollama Local":
        from browser_use.llm.ollama.chat import ChatOllama

        host = _normalize_ollama_local_host(secret)
        return ChatOllama(model=model, host=host)
    if provider == "Ollama Cloud":
        from browser_use.llm.ollama.chat import ChatOllama

        return ChatOllama(
            model=model,
            host="https://ollama.com",
            client_params={"headers": {"Authorization": f"Bearer {secret}"}},
        )
    if provider == "OpenRouter":
        from browser_use.llm.openai.chat import ChatOpenAI

        ref = (getattr(settings, "OPENROUTER_HTTP_REFERER", None) or "").strip()
        headers = {"X-Title": "ResumeElite"}
        if ref:
            headers["HTTP-Referer"] = ref
        return ChatOpenAI(
            model=model,
            api_key=secret,
            base_url="https://openrouter.ai/api/v1",
            default_headers=headers,
        )

    raise ValueError(f"Provider {provider!r} is not supported by the generic apply agent.")


def run_generic_fill(ctx: ApplyContext) -> FillResult:
    """Fill an unknown-ATS form via browser-use, stopping before submit."""
    if not is_available():
        detail = unavailability_reason()
        hint = (
            "Upgrade in the Huey worker venv: pip install 'browser-use>=0.13.0' "
            "then restart run_huey."
        )
        message = "Generic agent unavailable: browser-use could not be imported."
        if detail:
            message += f" ({detail})"
        message += f" {hint}"
        return FillResult(ok=False, error_code="no_adapter", message=message)

    answers = {
        "full_name": ctx.full_name,
        "email": ctx.email,
        "phone": ctx.phone,
        "location": ctx.location,
        "linkedin": ctx.linkedin_url,
        "website": ctx.website_url,
    }
    answers.update({str(k): v for k, v in (ctx.custom_qa or {}).items() if v not in ("", None)})
    payload = {k: v for k, v in answers.items() if v not in ("", None)}

    try:
        result = _drive_browser_use(ctx)
    except ValueError as e:
        return FillResult(ok=False, payload=payload, error_code="no_adapter", message=str(e))
    except Exception as e:  # noqa: BLE001 - any agent failure is non-fatal
        logger.warning("[apply_agent.generic] browser-use run failed: %s", e)
        return FillResult(
            ok=False,
            payload=payload,
            error_code="fill_failed",
            message=f"Generic agent error: {e}",
        )

    ctx.log("generic_fill", message="browser-use fill complete", action_snapshot={"ok": result})
    return FillResult(
        ok=bool(result),
        confidence=0.5 if result else 0.0,
        payload=payload,
        error_code="" if result else "fill_failed",
        message="" if result else "Generic agent did not complete the form.",
    )


async def _cleanup_browser_use_agent(agent: object) -> None:
    """Best-effort teardown of browser-use browser/session resources."""
    close_agent = getattr(agent, "close", None)
    if callable(close_agent):
        try:
            result = close_agent()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    for attr in ("browser_session", "browser", "context"):
        obj = getattr(agent, attr, None)
        if obj is None:
            continue
        for method_name in ("stop", "close", "kill"):
            method = getattr(obj, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass


def _make_browser_step_callback(ctx: ApplyContext):
    """Log each browser-use step (goal, URL, eval) and save screenshots when available."""
    from asgiref.sync import sync_to_async

    from .step_capture import save_step_screenshot

    counter = {"n": 0}

    def _persist_step(
        *,
        step_num: int,
        n: int,
        snapshot: dict,
        screenshot_path: str,
        message: str,
    ) -> None:
        ctx.log(
            "browser_step",
            message=message[:500],
            action_snapshot=snapshot,
            screenshot_path=screenshot_path,
        )

    async def _on_step(state_summary, agent_output, step_num: int) -> None:
        counter["n"] += 1
        n = counter["n"]
        st = getattr(agent_output, "current_state", None) if agent_output is not None else None
        page_info = getattr(state_summary, "page_info", None)
        url = ""
        if page_info is not None:
            url = getattr(page_info, "url", "") or ""
        snapshot = {
            "step": step_num,
            "url": url,
            "next_goal": getattr(st, "next_goal", "") if st else "",
            "memory": getattr(st, "memory", "") if st else "",
            "evaluation": getattr(st, "evaluation_previous_goal", "") if st else "",
        }
        screenshot_path = ""
        if ctx.attempt_id:
            raw_shot = getattr(state_summary, "screenshot", None)
            if not raw_shot and hasattr(state_summary, "get_screenshot"):
                try:
                    raw_shot = state_summary.get_screenshot()
                except Exception:
                    raw_shot = None
            if not raw_shot:
                raw_shot = getattr(state_summary, "screenshot_path", None)
            if raw_shot:
                screenshot_path = save_step_screenshot(ctx.attempt_id, f"step_{n:03d}", raw_shot)
        message = snapshot["next_goal"] or snapshot["evaluation"] or f"Browser step {step_num}"
        await sync_to_async(_persist_step, thread_sensitive=True)(
            step_num=step_num,
            n=n,
            snapshot=snapshot,
            screenshot_path=screenshot_path,
            message=message,
        )

    return _on_step


def _drive_browser_use(ctx: ApplyContext) -> bool:
    """Run a bounded browser-use agent. Returns True if it completed without error."""
    from browser_use import Agent

    llm = _build_browser_use_llm(ctx.user)
    task = _build_task_instructions(ctx)
    on_step = _make_browser_step_callback(ctx)
    from browser_use import BrowserProfile

    from .browser import apply_browser_headless

    browser_profile = BrowserProfile(
        headless=apply_browser_headless(),
        enable_default_extensions=False,
    )

    async def _run() -> bool:
        agent = Agent(
            task=task,
            llm=llm,
            browser_profile=browser_profile,
            register_new_step_callback=on_step,
            enable_planning=False,
            directly_open_url=True,
        )
        try:
            await asyncio.wait_for(
                agent.run(max_steps=MAX_AGENT_STEPS),
                timeout=GENERIC_AGENT_TIMEOUT_SECONDS,
            )
            return True
        finally:
            await _cleanup_browser_use_agent(agent)

    try:
        return asyncio.run(_run())
    finally:
        from .browser import kill_orphan_chromium

        kill_orphan_chromium()
