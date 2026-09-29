from django.core.cache import cache
from django.utils import timezone
from django.db import models
from django.conf import settings
from huey import crontab
from huey.contrib.djhuey import db_task, db_periodic_task

from .models import (
    AppAutomationSettings,
    OptimizedResume,
    AgentLog,
    LLMProviderConfig,
    LLMProviderPreference,
    UserResume,
    JobSearchTask,
    JobSearchTaskRun,
    PipelineEntry,
    JobListing,
    JobListingAction,
    Track,
    JobListingTrackMetrics,
    JobDescription,
    OptimizerWorkflow,
)
from datetime import timedelta
from .job_sources import normalize_site_names
from .job_search_core import (
    run_job_search_core,
    persist_preference_metrics_for_jobs,
)
from .agents import (
    create_workflow,
    DEFAULT_WRITER_PROMPT,
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    run_matching,
    VALID_STEP_IDS,
)
try:
    from .agents import create_workflow_from_steps
except ImportError:
    create_workflow_from_steps = None  # older agents.py without configurable workflow
from .crypto import decrypt_api_key
from .services import parse_pdf
from .callbacks import TokenUsageCallback
from .llm import (
    NO_CLOUD_LLM_MESSAGE,
    USAGE_QUERY_PIPELINE_VETTING,
    cloud_llm_available,
    get_active_llm_provider,
    get_llm,
    get_runtime_provider_candidates,
    is_auth_error,
    list_models_for_provider,
    local_llm_available,
    preference_candidates_available,
)
from .pipeline_llm_skill_extract import resolve_provider_api_key
from .prompt_store import (
    build_optimizer_graph_prompt_state,
    profile_for_llm,
    resolve_prompt_parts,
)
from .optimizer_budget import build_optimizer_context_state
import time
import logging

logger = logging.getLogger(__name__)

from django.contrib.auth import get_user_model


def _task_user(user_id: int):
    return get_user_model().objects.get(pk=int(user_id))

huey_logger = logging.getLogger("huey")


#
# Vetting matching (resume vs job) evaluation
#
VETTING_MATCHING_LOCK_KEY = "vetting_matching_task_running"  # Legacy global fallback
VETTING_MATCHING_LOCK_TIMEOUT = 3600  # 1 hour max


def get_vetting_matching_lock_key(user_id: int | str) -> str:
    """Per-tenant lock key to ensure task concurrency across multiple tenants."""
    return f"vetting_matching_task_running:u{user_id}"


RESUME_MATCHING_SNIPPET_CHARS = 8000
VETTING_MATCHING_JD_MIN_CHARS = 2000
# When interview_probability stays None (parse/model failure), do not enqueue again until this many hours pass
# unless the track's latest resume changed (handled separately).
VETTING_MATCHING_RETRY_COOLDOWN_HOURS = 6


def apply_vetting_to_applying_promotions(user, entry_ids: list[int] | None = None) -> int:
    """
    Move VETTING entries to APPLYING when interview probability >= configured minimum.
    If entry_ids is set, only those ids are considered (still must match stage/score rules).

    Does not enqueue resume optimization (manual Optimize on the Applying board only).
    """
    cfg = AppAutomationSettings.get_for_user(user)
    if not cfg.vetting_to_applying_enabled:
        return 0
    qs = PipelineEntry.objects.for_user(user).filter(
        stage=PipelineEntry.Stage.VETTING,
        removed_at__isnull=True,
        vetting_interview_probability__isnull=False,
    )
    if entry_ids is not None:
        qs = qs.filter(id__in=entry_ids)
    n = 0
    for entry in qs:
        if entry.meets_vetting_promotion_threshold(cfg):
            entry.move_to_applying(save=True)
            n += 1
    return n


def validate_cron(cron_string: str) -> None:
    """Validate cron expression. Raises ValueError if invalid."""
    if not cron_string or not isinstance(cron_string, str):
        raise ValueError("Cron expression is required.")
    try:
        import croniter
        croniter.croniter(cron_string)
    except Exception as e:
        raise ValueError(f"Invalid cron expression: expected 5 fields (minute hour day month weekday). {e}") from e


def _build_llm_override(llm_provider: str, llm_model: str | None, user):
    api_key = resolve_provider_api_key(llm_provider, user=user)
    if not api_key:
        raise ValueError(f"No API key configured for {llm_provider}.")

    available_models = list_models_for_provider(llm_provider, api_key)
    if not available_models:
        raise ValueError(f"No models available for {llm_provider}.")

    if llm_model and llm_model in available_models:
        model_to_use = llm_model
    else:
        # Try to use highest priority model from configured preferences
        model_to_use = None
        prefs = (
            LLMProviderPreference.objects.filter(
                provider_config__owner=user,
                provider_config__provider=llm_provider,
            )
            .select_related("provider_config")
            .order_by("priority", "id")
        )
        for pref in prefs:
            candidate_model = (pref.model or pref.provider_config.default_model or "").strip()
            if candidate_model and candidate_model in available_models:
                model_to_use = candidate_model
                break
        
        # Fall back to first available if no preference match found
        if not model_to_use:
            model_to_use = available_models[0]
        
        if llm_model:
            logger.warning(
                "LLM model %r not available for provider %s; using highest priority %s instead",
                llm_model,
                llm_provider,
                model_to_use,
            )

    return get_llm(llm_provider, api_key, model_to_use)


def get_next_run_at(cron_string: str, from_time=None):
    """Return next run datetime from cron string. from_time defaults to now (timezone-aware)."""
    from datetime import datetime
    import croniter
    if from_time is None:
        from_time = timezone.now()
    if timezone.is_naive(from_time):
        from_time = timezone.make_aware(from_time)
    it = croniter.croniter(cron_string, from_time)
    next_dt = it.get_next(datetime)
    if timezone.is_naive(next_dt):
        next_dt = timezone.make_aware(next_dt)
    return next_dt


# Lock key so only one job-search task runs at a time per user/tenant (avoids overlap)
JOB_SEARCH_TASK_LOCK_KEY = "job_search_task_running"  # Legacy global fallback
JOB_SEARCH_TASK_LOCK_TIMEOUT = 3600  # 1 hour max


def get_job_search_task_lock_key(user_id: int | str) -> str:
    """Per-tenant lock key enabling concurrent search execution across distinct tenants."""
    return f"job_search_task_running:u{user_id}"


CLEANUP_STATUS_CACHE_KEY = "cleanup_inactive_pipeline_entries_last_status"
CLEANUP_STATUS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _create_agent_log(optimized_resume, steps, prev_node, accumulated):
    """Create one AgentLog for the completed node; resolve step_display from steps if step_*."""
    from .agents import pop_node_llm_debug

    step_display = prev_node
    if steps and prev_node and prev_node.startswith("step_"):
        try:
            idx = int(prev_node.split("_", 1)[1])
            if 0 <= idx < len(steps):
                step_display = steps[idx]
        except (ValueError, IndexError):
            pass
    thought = dict(accumulated.get(prev_node) or {})
    # LangGraph stream updates often omit ephemeral debug keys; side-channel is authoritative.
    debug = pop_node_llm_debug(str(optimized_resume.id))
    if debug:
        for key, val in debug.items():
            if key == "feedback" and thought.get("feedback"):
                continue
            if val is not None:
                thought[key] = val
    AgentLog.objects.create(
        optimized_resume=optimized_resume,
        step_name=step_display,
        thought=thought,
    )


@db_task()
def optimize_resume_task(
    user_id,
    resume_id,
    job_description_id,
    provider,
    api_key,
    model=None,
    prompts=None,
    debug=False,
    rate_limit_delay=0,
    max_iterations=3,
    score_threshold=85,
    workflow_steps=None,
    loop_to=None,
    ats_judge_profile_id=None,
):
    """
    Huey async task: run the resume optimizer workflow for a single OptimizedResume.

    Fetches OptimizedResume once at top; on any failure sets status to failed.
    """
    user = _task_user(user_id)
    try:
        optimized_resume = OptimizedResume.objects.for_user(user).select_related(
            "optimizer_workflow",
            "optimizer_workflow__ats_judge_profile",
            "ats_judge_profile",
        ).get(id=resume_id)
    except OptimizedResume.DoesNotExist:
        return {"status": "error", "message": "OptimizedResume not found"}

    try:
        from .agents import clear_node_llm_debug

        if not cloud_llm_available(user):
            raise RuntimeError(NO_CLOUD_LLM_MESSAGE)

        clear_node_llm_debug(str(resume_id))
        job_desc = optimized_resume.job_description.content

        # Parse PDF
        resume_text = parse_pdf(optimized_resume.original_resume.file.path)

        ctx_state = build_optimizer_context_state(
            optimized=optimized_resume,
            resume_text_full=resume_text or "",
            job_description_full=job_desc or "",
        )

        # Setup Graph: configurable steps or default Writer -> ATS -> Recruiter
        steps = workflow_steps if workflow_steps else ["writer", "ats_judge", "recruiter_judge"]
        if workflow_steps:
            if create_workflow_from_steps is None:
                raise ValueError(
                    "workflow_steps is not supported: resume_app.agents has no create_workflow_from_steps. "
                    "Update agents.py with the configurable workflow implementation."
                )
            app = create_workflow_from_steps(
                steps,
                max_iterations=max(1, min(int(max_iterations), 5)),
                loop_to=loop_to,
            )
        else:
            app = create_workflow()

        # Initial State (prompts from profile + optional API override)
        workflow = optimized_resume.optimizer_workflow
        explicit_ats = ats_judge_profile_id or optimized_resume.ats_judge_profile_id
        _pb = build_optimizer_graph_prompt_state(
            prompts or None,
            ats_judge_profile_id=explicit_ats,
            workflow=workflow,
            user=user,
        )
        initial_state = {
            **ctx_state,
            "optimized_resume": "",
            "ats_score": 0,
            "recruiter_score": 0,
            "feedback": [],
            "iteration_count": 0,
            "llm": None,
            "job_cache_key": str(resume_id),
            "writer_prompt_template": _pb["writer_prompt_template"],
            "writer_prompt_system": _pb["writer_prompt_system"],
            "writer_prompt_user": _pb["writer_prompt_user"],
            "writer_prompt_legacy": _pb["writer_prompt_legacy"],
            "ats_judge_prompt_template": _pb["ats_judge_prompt_template"],
            "ats_judge_prompt_system": _pb["ats_judge_prompt_system"],
            "ats_judge_prompt_user": _pb["ats_judge_prompt_user"],
            "ats_judge_prompt_legacy": _pb["ats_judge_prompt_legacy"],
            "recruiter_judge_prompt_template": _pb["recruiter_judge_prompt_template"],
            "recruiter_judge_prompt_system": _pb["recruiter_judge_prompt_system"],
            "recruiter_judge_prompt_user": _pb["recruiter_judge_prompt_user"],
            "recruiter_judge_prompt_legacy": _pb["recruiter_judge_prompt_legacy"],
            "debug": bool(debug),
            "max_iterations": max(1, min(int(max_iterations), 5)),
            "score_threshold": max(0, min(100, int(score_threshold))),
            "user_id": user_id,
        }

        # Run Graph
        last_state = initial_state
        optimized_resume.status = OptimizedResume.STATUS_RUNNING
        optimized_resume.save(update_fields=["status"])
        usage_callback = TokenUsageCallback()
        # recursion_limit must exceed the number of nodes: LangGraph counts each node invocation
        # and the transition to END; with limit=num_steps the last node can hit the limit before finishing.
        num_steps = len(steps) if steps else 3
        # LangGraph 1.x counts graph steps aggressively; too low a limit can abort before END.
        run_config = {
            "callbacks": [usage_callback],
            "recursion_limit": max(100, num_steps * 25),
        }
        # Coalesce: stream() can yield multiple times per node; only log once per node with merged state
        accumulated = {}
        prev_node = None
        CANCELLED_MESSAGE = "Cancelled by user"
        for output in app.stream(initial_state, config=run_config, stream_mode="updates"):
            optimized_resume.refresh_from_db()
            if optimized_resume.status == OptimizedResume.STATUS_FAILED and (optimized_resume.error_message or "").strip() == CANCELLED_MESSAGE:
                break
            for key, state_update in output.items():
                # LangGraph may yield (node_name, chunk_index) or similar; use first element as logical node
                node_name = key[0] if isinstance(key, (list, tuple)) else key
                node_name = str(node_name) if not isinstance(node_name, str) else node_name
                last_state.update(state_update)
                if prev_node is not None and node_name != prev_node:
                    _create_agent_log(optimized_resume, steps, prev_node, accumulated)
                if node_name not in accumulated:
                    accumulated[node_name] = {}
                accumulated[node_name].update(state_update)
                prev_node = node_name

                update_fields: list[str] = []
                if state_update.get("jd_cleansed") is not None or (
                    "writer_job_description" in state_update
                    and "optimized_resume" not in state_update
                    and "ats_score" not in state_update
                    and "recruiter_score" not in state_update
                ):
                    parse_info = state_update.get("parse_info") or {}
                    in_c = parse_info.get("input_chars")
                    out_c = parse_info.get("output_chars")
                    if in_c is not None and out_c is not None:
                        optimized_resume.status_display = f"Cleansing JD ({in_c}→{out_c} chars)"
                    else:
                        optimized_resume.status_display = "Cleansing job description"
                    update_fields.append("status_display")
                elif "optimized_resume" in state_update:
                    optimized_resume.status_display = "Drafting"
                    update_fields.append("status_display")
                    oc = last_state.get("optimized_resume")
                    if isinstance(oc, str) and oc.strip():
                        optimized_resume.optimized_content = oc
                        update_fields.append("optimized_content")
                elif "ats_score" in state_update or "recruiter_score" in state_update:
                    optimized_resume.status_display = (
                        f"Scoring: ATS={last_state.get('ats_score', 'N/A')}, "
                        f"Recruiter={last_state.get('recruiter_score', 'N/A')}"
                    )
                    update_fields.append("status_display")
                    if "ats_score" in state_update:
                        optimized_resume.ats_score = last_state.get("ats_score")
                        update_fields.append("ats_score")
                    if "recruiter_score" in state_update:
                        optimized_resume.recruiter_score = last_state.get("recruiter_score")
                        update_fields.append("recruiter_score")
                if update_fields:
                    optimized_resume.save(update_fields=list(dict.fromkeys(update_fields)))

                if rate_limit_delay and float(rate_limit_delay) > 0:
                    time.sleep(float(rate_limit_delay))
        if prev_node is not None:
            _create_agent_log(optimized_resume, steps, prev_node, accumulated)

        optimized_resume.refresh_from_db()
        if optimized_resume.status == OptimizedResume.STATUS_FAILED and (optimized_resume.error_message or "").strip() == CANCELLED_MESSAGE:
            clear_node_llm_debug(str(resume_id))
            return {"status": "cancelled", "resume_id": resume_id}

        # Final update
        optimized_resume.optimized_content = last_state['optimized_resume']
        optimized_resume.status = OptimizedResume.STATUS_COMPLETED
        optimized_resume.ats_score = last_state.get('ats_score')
        optimized_resume.recruiter_score = last_state.get('recruiter_score')
        optimized_resume.status_display = ""
        optimized_resume.total_input_tokens = usage_callback.total_input_tokens or None
        optimized_resume.total_output_tokens = usage_callback.total_output_tokens or None
        snap = last_state.get("optimizer_context_budget")
        if isinstance(snap, dict):
            optimized_resume.optimizer_context_snapshot = snap
        optimized_resume.save()

        from .domain.event_bus import event_bus
        from .domain.events import ResumeOptimizationCompleted

        event_bus.publish(
            ResumeOptimizationCompleted(
                user_id=user.id,
                optimized_resume_id=optimized_resume.id,
                pipeline_entry_id=optimized_resume.pipeline_entry_id,
                ats_score=optimized_resume.ats_score,
                recruiter_score=optimized_resume.recruiter_score,
            )
        )

        clear_node_llm_debug(str(resume_id))
        return {"status": "success", "resume_id": resume_id}

    except Exception as e:
        from .agents import clear_node_llm_debug

        clear_node_llm_debug(str(resume_id))
        if is_auth_error(e):
            LLMProviderConfig.objects.for_user(user).filter(provider=provider).update(encrypted_api_key="", last_validated_at=None)
        optimized_resume.refresh_from_db()
        optimized_resume.status = OptimizedResume.STATUS_FAILED
        optimized_resume.error_message = str(e)
        optimized_resume.save()
        return {"status": "error", "message": str(e)}


PIPELINE_OPT_MIN_JD_CHARS = 50


def _build_pipeline_job_description(job: JobListing, user) -> str:
    from .jd_cleanser import JDCleanserService

    title = (job.title or "").strip()
    company = (job.company_name or "").strip()
    loc = (job.location or "").strip()
    header = "\n".join(
        x
        for x in (
            f"Title: {title}" if title else "",
            f"Company: {company}" if company else "",
            f"Location: {loc}" if loc else "",
        )
        if x
    )
    # Use JDCleanserService to strip boilerplate before sending to LLM.
    # Local Ollama cleanses the text; falls back to heuristics if unavailable to avoid cloud token usage.
    desc = JDCleanserService.cleanse((job.description or ""), title=title, use_llm=True, user=user, only_local=True)
    url = (job.url or "").strip()
    tail_parts = [p for p in (desc, f"URL: {url}" if url else "") if p]
    tail = "\n\n".join(tail_parts)
    if header and tail:
        return f"{header}\n\n{tail}".strip()
    return (header or tail).strip()


def _resolve_user_resume_for_track(user, track_slug: str) -> UserResume | None:
    track_slug = (track_slug or "").strip().lower()
    latest_track = (
        UserResume.objects.for_user(user).filter(is_library=True, track=track_slug).order_by("-uploaded_at").first()
        if track_slug
        else None
    )
    if latest_track and latest_track.file:
        return latest_track
    latest = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()
    if latest and latest.file:
        return latest
    return None


def _resolve_llm_for_pipeline_optimization(user) -> tuple[str | None, LLMProviderConfig | None]:
    provider = get_active_llm_provider(user, None)
    config = None
    if provider:
        config = (
            LLMProviderConfig.objects.for_user(user).filter(provider=provider)
            .exclude(encrypted_api_key="")
            .first()
        )
    if not config:
        cands = get_runtime_provider_candidates(user)
        if cands:
            cand = cands[0]
            config = cand["config"]
            provider = cand["provider"]
    return provider, config


def _enqueue_single_pipeline_resume_optimization(
    pipeline_entry_id: int,
    *,
    force_new: bool,
) -> dict:
    """
    Create JobDescription + OptimizedResume for a pipeline entry and enqueue optimize_resume_task.
    When force_new is False, skip if another run for this entry is queued or running.
    """
    try:
        entry = (
            PipelineEntry.objects.filter(
                id=pipeline_entry_id,
                removed_at__isnull=True,
            )
            .select_related("job_listing")
            .first()
        )
    except Exception:
        entry = None
    if not entry:
        return {"status": "error", "message": "Pipeline entry not found", "entry_id": pipeline_entry_id}
    if entry.stage != PipelineEntry.Stage.APPLYING:
        return {
            "status": "skipped",
            "message": "Entry is not in Applying stage",
            "entry_id": pipeline_entry_id,
        }

    user = entry.owner

    if not force_new:
        active = OptimizedResume.objects.for_user(user).filter(
            pipeline_entry_id=entry.id,
            status__in=(OptimizedResume.STATUS_QUEUED, OptimizedResume.STATUS_RUNNING),
        ).exists()
        if active:
            return {
                "status": "skipped",
                "message": "Optimization already queued or running",
                "entry_id": pipeline_entry_id,
            }

    job = entry.job_listing
    jd_text = _build_pipeline_job_description(job, user)
    if len(jd_text) < PIPELINE_OPT_MIN_JD_CHARS:
        return {
            "status": "error",
            "message": f"Job description too short (min {PIPELINE_OPT_MIN_JD_CHARS} chars)",
            "entry_id": pipeline_entry_id,
        }

    user_resume = _resolve_user_resume_for_track(user, entry.track)
    if not user_resume or not user_resume.file:
        return {
            "status": "error",
            "message": "No resume PDF for this search profile (upload and assign one under Search profiles)",
            "entry_id": pipeline_entry_id,
        }

    provider, config = _resolve_llm_for_pipeline_optimization(user)
    if not provider or not config:
        return {
            "status": "error",
            "message": "No LLM configured with API key",
            "entry_id": pipeline_entry_id,
        }
    api_key = decrypt_api_key(config.encrypted_api_key or "")
    if not (api_key or "").strip():
        return {
            "status": "error",
            "message": "No API key for active LLM provider",
            "entry_id": pipeline_entry_id,
        }
    model = (config.default_model or "").strip() or None

    solo = AppAutomationSettings.get_for_user(user)
    workflow: OptimizerWorkflow | None = solo.applying_optimizer_workflow
    workflow_steps = None
    loop_to = None
    max_iterations = 3
    score_threshold = 85
    if workflow:
        if workflow.steps and isinstance(workflow.steps, list):
            workflow_steps = [
                s for s in workflow.steps if isinstance(s, str) and s in VALID_STEP_IDS
            ]
            if not workflow_steps:
                workflow_steps = None
        loop_to = (workflow.loop_to or "").strip() or None
        if loop_to and loop_to not in VALID_STEP_IDS:
            loop_to = None
        if workflow.max_iterations:
            max_iterations = max(1, min(int(workflow.max_iterations), 5))
        if workflow.score_threshold is not None:
            st = int(workflow.score_threshold)
            score_threshold = max(0, min(100, st))

    from .prompt_store import resolve_effective_ats_judge_profile_id, get_ats_judge_profile_by_id

    effective_ats_id = resolve_effective_ats_judge_profile_id(workflow=workflow, user=user)
    ats_profile = get_ats_judge_profile_by_id(effective_ats_id, user=user)

    jd = JobDescription.objects.create(content=jd_text)
    opt = OptimizedResume.objects.create(
        owner=user,
        original_resume=user_resume,
        job_description=jd,
        status=OptimizedResume.STATUS_QUEUED,
        pipeline_entry=entry,
        optimizer_workflow=workflow,
        ats_judge_profile=ats_profile,
    )

    optimize_resume_task(
        user.id,
        opt.id,
        jd.id,
        provider,
        api_key or "",
        model,
        prompts=None,
        debug=True,
        workflow_steps=workflow_steps,
        loop_to=loop_to,
        max_iterations=max_iterations,
        score_threshold=score_threshold,
        ats_judge_profile_id=effective_ats_id,
    )
    return {
        "status": "ok",
        "entry_id": pipeline_entry_id,
        "optimized_resume_id": opt.id,
    }


@db_task()
def enqueue_applying_resume_optimization_task(
    user_id,
    pipeline_entry_ids: list[int],
    force_new: bool = False,
):
    """Huey: enqueue resume optimization for Applying-stage pipeline entries."""
    if not pipeline_entry_ids:
        return {"status": "skipped", "message": "No entry ids"}
    results = []
    for eid in pipeline_entry_ids:
        try:
            eid_int = int(eid)
        except (TypeError, ValueError):
            results.append({"status": "error", "message": "Invalid id", "entry_id": eid})
            continue
        results.append(_enqueue_single_pipeline_resume_optimization(eid_int, force_new=force_new))
    return {"status": "success", "results": results}


@db_task()
def run_job_search_task(user_id, task_id):
    """
    Huey task: run one JobSearchTask (fetch, filter, rank, add to pipeline).
    Uses a per-tenant lock so concurrent job searches for different users run in parallel.
    """
    lock_key = get_job_search_task_lock_key(user_id)
    if not cache.add(lock_key, 1, JOB_SEARCH_TASK_LOCK_TIMEOUT):
        logger.warning(
            "[run_job_search_task] Skipping task %s: another job search task is running for user %s",
            task_id,
            user_id,
        )
        return {"status": "skipped", "message": "Another job search task is running for this user"}
    try:
        return _run_job_search_task_impl(user_id, task_id)
    finally:
        cache.delete(lock_key)


def _prune_old_runs(task):
    """Keep only the last 5 runs for a task, and keep detailed job audit only for the last 2 runs."""
    try:
        run_ids = list(
            JobSearchTaskRun.objects.filter(task=task)
            .order_by("-started_at", "-id")
            .values_list("id", flat=True)
        )
        if len(run_ids) > 5:
            delete_ids = run_ids[5:]
            JobSearchTaskRun.objects.filter(task=task, id__in=delete_ids).delete()
            run_ids = run_ids[:5]

        # For runs older than the last 2, clear details to conserve storage
        if len(run_ids) > 2:
            clear_ids = run_ids[2:]
            JobSearchTaskRun.objects.filter(task=task, id__in=clear_ids).update(details=[])
    except Exception:
        logger.exception("[run_job_search_task] Pruning old runs failed for task_id=%s", task.id)


def _run_job_search_task_impl(user_id, task_id):
    user = _task_user(user_id)
    try:
        task = JobSearchTask.objects.for_user(user).get(id=task_id)
    except JobSearchTask.DoesNotExist:
        return {"status": "error", "message": "JobSearchTask not found"}
    if not task.is_active:
        return {"status": "skipped", "message": "Task is inactive"}

    run = JobSearchTaskRun.objects.create(
        task=task,
        status=JobSearchTaskRun.STATUS_RUNNING,
    )
    try:
        jobs_fetched, jobs_after_filter, jobs_out, _refs, audit_items = run_job_search_core(
            user=user,
            search_term=task.search_term,
            location=task.location or None,
            track=task.track or None,
            results_wanted=task.jobs_to_fetch,
            site_name=normalize_site_names(
                task.site_name if isinstance(task.site_name, list) else None
            ),
            return_audit_log=True,
        )
    except Exception as e:
        logger.exception("[run_job_search_task] task_id=%s core failed: %s", task_id, e)
        run.status = JobSearchTaskRun.STATUS_FAILED
        run.finished_at = timezone.now()
        run.error_message = str(e)
        run.details = []
        run.save()
        _prune_old_runs(task)
        return {"status": "error", "message": str(e)}

    audit_by_id = {item["job_id"]: item for item in audit_items}
    jobs_added_to_pipeline = 0
    from .search_profile_scope import upsert_pipeline_entry

    for payload in jobs_out:
        job_id = payload.id
        pe = PipelineEntry.objects.for_user(user).filter(job_listing_id=job_id, track=task.track).first()
        audit_entry = audit_by_id.get(job_id)
        if pe is None:
            upsert_pipeline_entry(
                user,
                job_listing_id=job_id,
                slug=task.track,
                defaults={"stage": PipelineEntry.Stage.PIPELINE},
            )
            jobs_added_to_pipeline += 1
            if audit_entry:
                audit_entry["disposition"] = "saved_to_pipeline"
                audit_entry["reason"] = "Saved to Pipeline (New)"
        elif pe.removed_at is not None:
            repost_cooldown_days = getattr(settings, "PIPELINE_DELETED_REPOST_COOLDOWN_DAYS", 14)
            repost_cooldown = timedelta(days=repost_cooldown_days)
            now = timezone.now()

            is_reposted = False
            posted_at = getattr(payload, "posted_at", None)
            if not posted_at:
                listing = JobListing.objects.filter(id=job_id).first()
                if listing and listing.posted_at:
                    posted_at = listing.posted_at

            if posted_at and pe.removed_at:
                if (now - pe.removed_at) >= repost_cooldown:
                    posted_at_dt = posted_at
                    if timezone.is_naive(posted_at_dt):
                        posted_at_dt = timezone.make_aware(posted_at_dt)
                    if posted_at_dt > pe.removed_at:
                        is_reposted = True

            if is_reposted:
                pe.removed_at = None
                pe.stage = PipelineEntry.Stage.PIPELINE
                pe.save(update_fields=["removed_at", "stage"])
                jobs_added_to_pipeline += 1
                if audit_entry:
                    audit_entry["disposition"] = "reposted_to_pipeline"
                    date_str = posted_at.strftime("%b %d, %Y") if hasattr(posted_at, "strftime") else str(posted_at)
                    audit_entry["reason"] = (
                        f"Re-admitted to Pipeline: Reposted on {date_str} (after {repost_cooldown_days}-day cooldown)"
                    )
            else:
                if audit_entry:
                    audit_entry["disposition"] = "previously_removed"
                    audit_entry["reason"] = "Skipped: Previously removed / archived from pipeline"
        else:
            if audit_entry:
                audit_entry["disposition"] = "already_in_pipeline"
                stage_name = pe.get_stage_display() if hasattr(pe, "get_stage_display") else pe.stage
                audit_entry["reason"] = f"Skipped: Already in pipeline (Stage: {stage_name})"


    try:
        from .job_dedupe import dedupe_pipeline_entries

        dedupe_result = dedupe_pipeline_entries(
            user=user,
            track_slug=task.track,
            stage="pipeline",
            include_done=False,
        )
        if dedupe_result.get("entries_removed"):
            logger.info(
                "[run_job_search_task] post-search dedupe track=%s removed=%s groups=%s",
                task.track,
                dedupe_result.get("entries_removed"),
                dedupe_result.get("duplicate_groups"),
            )
    except Exception:
        logger.exception("[run_job_search_task] post-search dedupe failed task_id=%s", task_id)

    if jobs_out and task.track:
        try:
            job_ids = [p.id for p in jobs_out]
            jobs = list(JobListing.objects.filter(id__in=job_ids))
            persist_preference_metrics_for_jobs(
                user=user,
                track=task.track,
                job_listings=jobs,
                payloads=jobs_out,
            )
        except Exception:
            logger.exception(
                "[run_job_search_task] persist preference metrics failed task_id=%s",
                task_id,
            )
        try:
            apply_pipeline_auto_promotions(user)
        except Exception:
            logger.exception(
                "[run_job_search_task] apply_pipeline_auto_promotions failed task_id=%s",
                task_id,
            )

    run.jobs_fetched = jobs_fetched
    run.jobs_after_filter = jobs_after_filter
    run.jobs_added_to_pipeline = jobs_added_to_pipeline
    run.status = JobSearchTaskRun.STATUS_COMPLETED
    run.finished_at = timezone.now()
    run.details = audit_items
    run.save()
    _prune_old_runs(task)

    logger.info(
        "[run_job_search_task] task_id=%s done: fetched=%d after_filter=%d added=%d",
        task_id, jobs_fetched, jobs_after_filter, jobs_added_to_pipeline,
    )
    return {"status": "success", "task_id": task_id}


@db_task()
def evaluate_vetting_matching_task(
    user_id,
    pipeline_entry_ids: list[int],
    llm_provider: str | None = None,
    llm_model: str | None = None,
    matching_prompt: str | None = None,
):
    """
    Huey task: evaluate PipelineEntry rows in VETTING stage.

    Uses:
    - Matching prompt template (optional override)
    - Resume text from the latest `UserResume` associated with the entry's `track`
      (fallback: latest resume overall).
    Writes:
    - vetting_interview_probability
    - vetting_interview_reasoning
    - vetting_interview_resume_id (so we can skip if unchanged)

    Skips LLM when job description is shorter than VETTING_MATCHING_JD_MIN_CHARS (too little signal).
    """
    if not pipeline_entry_ids:
        return {"status": "skipped", "message": "No pipeline_entry_ids provided"}

    user = _task_user(user_id)
    lock_key = get_vetting_matching_lock_key(user.id)

    if not cache.add(lock_key, 1, VETTING_MATCHING_LOCK_TIMEOUT):
        logger.warning(
            "[evaluate_vetting_matching_task] Skipping: another vetting matching task is running for tenant %s (user_id=%s)",
            getattr(user, "username", str(user_id)),
            getattr(user, "id", user_id),
        )
        return {"status": "skipped", "message": "Another vetting matching task is running for this user"}

    try:
        entries = list(
            PipelineEntry.objects.for_user(user).filter(
                id__in=pipeline_entry_ids,
                stage=PipelineEntry.Stage.VETTING,
                removed_at__isnull=True,
            )
            .select_related("job_listing")
        )
        if not entries:
            return {"status": "skipped", "message": "No entries in VETTING stage"}

        from .llm import local_llm_available, preference_candidates_available

        llm_override = None
        if llm_provider:
            try:
                llm_override = _build_llm_override(llm_provider, llm_model, user)
            except Exception as e:
                logger.exception(
                    "[evaluate_vetting_matching_task] invalid LLM override tenant=%s (user_id=%s) provider=%s model=%s: %s",
                    getattr(user, "username", str(user_id)),
                    getattr(user, "id", user_id),
                    llm_provider,
                    llm_model,
                    e,
                )
                return {
                    "status": "error",
                    "message": f"Invalid LLM override: {e}",
                }
        elif not local_llm_available(user):
            tenant_label = getattr(user, "username", str(user_id))
            uid = getattr(user, "id", user_id)
            logger.warning(
                "[evaluate_vetting_matching_task] tenant=%s (user_id=%s): No local LLM (Ollama) configured or on cooldown. Skipping vetting matching.",
                tenant_label,
                uid,
            )
            return {
                "status": "skipped",
                "message": f"Tenant {tenant_label}: No local LLM (Ollama) configured or on cooldown.",
            }

        jd_max_chars = getattr(settings, "JOB_MATCHING_JD_MAX_CHARS", 12000)
        now = timezone.now()
        resume_snippet_map: dict[str, tuple[UserResume, str]] = {}

        # Parse each track's resume once per task.
        tracks = {e.track for e in entries if e.track}
        latest_overall = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()
        for track in tracks:
            latest = (
                UserResume.objects.for_user(user).filter(is_library=True, track=track).order_by("-uploaded_at").first()
                or latest_overall
            )
            if not latest or not latest.file:
                continue
            try:
                resume_text = parse_pdf(latest.file.path)
                resume_snippet_map[track] = (latest, resume_text[:RESUME_MATCHING_SNIPPET_CHARS])
            except Exception as e:
                logger.exception(
                    "[evaluate_vetting_matching_task] Could not parse resume (track=%s, resume_id=%s): %s",
                    track,
                    getattr(latest, "id", None),
                    e,
                )
                continue

        updated = 0
        skipped = 0
        errors = []
        ms, mu, ml = resolve_prompt_parts(profile_for_llm(None), "matching")
        for entry in entries:
            track = entry.track
            resolved = resume_snippet_map.get(track)
            if not resolved:
                skipped += 1
                continue
            resume_obj, resume_snippet = resolved

            # Skip if we already evaluated using the same resume.
            if (
                entry.vetting_interview_probability is not None
                and entry.vetting_interview_resume_id == resume_obj.id
            ):
                skipped += 1
                continue

            from .jd_cleanser import JDCleanserService
            from .job_search_core import effective_vetting_job_description

            raw_jd = effective_vetting_job_description(entry.job_listing, enrich=True)
            if not raw_jd or len(raw_jd) < VETTING_MATCHING_JD_MIN_CHARS:
                skipped += 1
                continue

            # Cleanse JD for vetting match to save tokens.
            # Local Ollama cleanses the text; falls back to heuristics if unavailable to avoid cloud token usage.
            jd = JDCleanserService.cleanse(raw_jd, title=entry.job_listing.title, use_llm=True, user=user, only_local=True)
            jd = jd[:jd_max_chars]

            try:
                # Retry a couple times: models sometimes omit interview_probability
                # even when requested via schema.
                result = None
                for _attempt in range(3):
                    if matching_prompt and str(matching_prompt).strip():
                        result = run_matching(
                            resume_snippet,
                            jd,
                            llm_override,
                            user=user,
                            prompt_template=matching_prompt,
                            job_cache_key=f"vetting:{entry.id}",
                            usage_query_kind=USAGE_QUERY_PIPELINE_VETTING,
                        )
                    else:
                        result = run_matching(
                            resume_snippet,
                            jd,
                            llm_override,
                            user=user,
                            prompt_system=ms,
                            prompt_user=mu,
                            prompt_legacy=ml or None,
                            job_cache_key=f"vetting:{entry.id}",
                            usage_query_kind=USAGE_QUERY_PIPELINE_VETTING,
                        )
                    if result.get("interview_probability") is not None:
                        break

                ip = (result or {}).get("interview_probability")
                reasoning = ((result or {}).get("reasoning") or "").strip()
                entry.record_vetting_interview_result(
                    probability=ip,
                    reasoning=reasoning,
                    resume_id=resume_obj.id,
                    scored_at=now,
                    save=True,
                )
                updated += 1
                apply_vetting_to_applying_promotions(user, [entry.id])
            except Exception as e:
                tenant_label = getattr(user, "username", str(user_id))
                uid = getattr(user, "id", user_id)
                logger.exception("[evaluate_vetting_matching_task] tenant=%s (user_id=%s) entry_id=%s failed: %s", tenant_label, uid, entry.id, e)
                errors.append(f"Entry {entry.id}: {e}")

        return {
            "status": "success",
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:20],
        }
    finally:
        cache.delete(lock_key)


def try_vetting_match_debug(
    entry: PipelineEntry | int,
    *,
    matching_prompt: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """
    Run exactly one vetting matching LLM call and return parsed fields plus raw model text.
    Does not update PipelineEntry. Used by the Vetting "Match debug" UI.

    Still records token usage under Pipeline — vetting match (same as automation).
    """
    if isinstance(entry, int):
        q = (
            PipelineEntry.objects.filter(pk=entry, removed_at__isnull=True)
            .select_related("job_listing")
            .first()
        )
        if not q:
            return {"ok": False, "skip_reason": "entry_not_found"}
        entry = q
    if entry.stage != PipelineEntry.Stage.VETTING:
        return {"ok": False, "skip_reason": "not_vetting_stage", "stage": entry.stage or ""}
    user = entry.owner
    track = entry.track
    latest_overall = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()
    latest = (
        UserResume.objects.for_user(user).filter(is_library=True, track=track).order_by("-uploaded_at").first()
        or latest_overall
    )
    if not latest or not latest.file:
        return {"ok": False, "skip_reason": "no_resume"}
    try:
        resume_text = parse_pdf(latest.file.path)
    except Exception as e:
        logger.exception("try_vetting_match_debug resume parse entry_id=%s", entry.id)
        return {"ok": False, "skip_reason": "resume_parse_error", "error": str(e)}
    resume_snippet = resume_text[:RESUME_MATCHING_SNIPPET_CHARS]
    from .job_search_core import effective_vetting_job_description

    jd = effective_vetting_job_description(entry.job_listing, enrich=True)
    jd_len = len(jd)
    if not jd or jd_len < VETTING_MATCHING_JD_MIN_CHARS:
        return {
            "ok": False,
            "skip_reason": "jd_too_short",
            "jd_len": jd_len,
            "jd_min": VETTING_MATCHING_JD_MIN_CHARS,
        }
    jd_max_chars = getattr(settings, "JOB_MATCHING_JD_MAX_CHARS", 12000)
    jd_use = jd[:jd_max_chars]

    llm = None
    if llm_provider:
        api_key = resolve_provider_api_key(llm_provider, user=user)
        if not api_key:
            return {
                "ok": False,
                "skip_reason": "no_api_key",
                "provider": llm_provider,
            }
        
        # Validate and resolve model if needed
        try:
            from .llm import get_llm
            
            available_models = list_models_for_provider(llm_provider, api_key)
            if not available_models:
                return {
                    "ok": False,
                    "skip_reason": "no_models_available",
                    "provider": llm_provider,
                }
            
            # Use requested model if available, else use highest priority configured model
            if llm_model and llm_model in available_models:
                model_to_use = llm_model
            else:
                # Try to use highest priority model from configured preferences
                model_to_use = None
                prefs = (
                    LLMProviderPreference.objects.filter(
                        provider_config__owner=user,
                        provider_config__provider=llm_provider,
                    )
                    .select_related("provider_config")
                    .order_by("priority", "id")
                )
                for pref in prefs:
                    candidate_model = (pref.model or pref.provider_config.default_model or "").strip()
                    if candidate_model and candidate_model in available_models:
                        model_to_use = candidate_model
                        break
                
                # Fall back to first available if no preference match found
                if not model_to_use:
                    model_to_use = available_models[0]
                
                if llm_model:
                    logger.warning(
                        "try_vetting_match_debug requested model %s not available for %s, using highest priority %s",
                        llm_model,
                        llm_provider,
                        model_to_use,
                    )
            
            llm = get_llm(llm_provider, api_key, model_to_use)
        except Exception as e:
            logger.exception(
                "try_vetting_match_debug llm init entry_id=%s provider=%s model=%s",
                entry.id,
                llm_provider,
                llm_model,
            )
            return {
                "ok": False,
                "skip_reason": "llm_init_error",
                "error": str(e),
            }

    ms, mu, ml = resolve_prompt_parts(profile_for_llm(None), "matching")
    try:
        if matching_prompt and str(matching_prompt).strip():
            result = run_matching(
                resume_snippet,
                jd_use,
                llm,
                user=user,
                prompt_template=matching_prompt,
                job_cache_key=f"vetting:debug:{entry.id}",
                usage_query_kind=USAGE_QUERY_PIPELINE_VETTING,
                return_debug=True,
            )
        else:
            result = run_matching(
                resume_snippet,
                jd_use,
                llm,
                user=user,
                prompt_system=ms,
                prompt_user=mu,
                prompt_legacy=ml or None,
                job_cache_key=f"vetting:debug:{entry.id}",
                usage_query_kind=USAGE_QUERY_PIPELINE_VETTING,
                return_debug=True,
            )
    except Exception as e:
        logger.exception("try_vetting_match_debug LLM entry_id=%s", entry.id)
        return {"ok": False, "skip_reason": "llm_error", "error": str(e)}
    return {
        "ok": True,
        "skip_reason": None,
        "jd_len": jd_len,
        "resume_snippet_len": len(resume_snippet),
        "resume_id": latest.id,
        "result": result,
    }


def apply_pipeline_auto_promotions(user) -> int:
    """
    Promote PIPELINE (or legacy blank stage) entries to VETTING when per-track Pref margin
    meets the configured minimum; enqueue vetting matching for promoted rows.
    """
    cfg = AppAutomationSettings.get_for_user(user)
    if not cfg.pipeline_to_vetting_enabled:
        return 0
    entries = (
        PipelineEntry.objects.for_user(user).filter(removed_at__isnull=True)
        .filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE))
        .only("id", "job_listing_id", "track")
    )
    promoted: list[int] = []
    for entry in entries:
        m = (
            JobListingTrackMetrics.objects.for_user(user).filter(
                job_listing_id=entry.job_listing_id,
                track=entry.track,
            )
            .only("preference_margin")
            .first()
        )
        margin = m.preference_margin if m else None
        if not entry.meets_pipeline_auto_promotion_threshold(margin, cfg):
            continue
        entry.move_to_vetting(save=True)
        promoted.append(entry.id)
    # Fast-track high-confidence matches directly to Applying
    fast_tracked = []
    normal_promoted = []

    for entry_id in promoted:
        entry = PipelineEntry.objects.for_user(user).get(id=entry_id)
        m = JobListingTrackMetrics.objects.for_user(user).filter(
            job_listing_id=entry.job_listing_id, track=entry.track
        ).first()
        margin = m.preference_margin if m else None

        if entry.is_fast_track_eligible(margin):
            entry.move_to_applying(save=True)
            fast_tracked.append(entry_id)
        else:
            normal_promoted.append(entry_id)

    if normal_promoted:
        evaluate_vetting_matching_task(
            user.id, normal_promoted, llm_provider=None, llm_model=None, matching_prompt=None
        )

    if fast_tracked:
        logger.info(
            "[apply_pipeline_auto_promotions] Fast-tracked %d high-confidence jobs to Applying",
            len(fast_tracked),
        )

    return len(promoted)


@db_task()
def process_user_vetting_matching_task(user_id: int):
    """
    Huey worker task: Evaluate due Vetting entries for a single user.
    Enables distributed, parallel vetting evaluation across Huey workers.
    """
    try:
        user = _task_user(user_id)
        _evaluate_due_vetting_matching_for_user(user)
    except Exception as e:
        logger.exception("[process_user_vetting_matching_task] failed for user_id=%s: %s", user_id, e)


def _evaluate_due_vetting_matching_for_user(user, max_to_enqueue: int = 20):
    candidate_entries = (
        PipelineEntry.objects.for_user(user).filter(
            stage=PipelineEntry.Stage.VETTING,
            removed_at__isnull=True,
        )
        .order_by("-added_at")[:200]
        .only(
            "id",
            "track",
            "vetting_interview_probability",
            "vetting_interview_resume_id",
            "vetting_interview_scored_at",
        )
    )
    entries = list(candidate_entries)
    if not entries:
        apply_vetting_to_applying_promotions(user)
        return

    latest_overall = UserResume.objects.for_user(user).filter(is_library=True).order_by("-uploaded_at").first()
    latest_by_track: dict[str, UserResume | None] = {}
    for track in {e.track for e in entries if e.track}:
        latest_by_track[track] = (
            UserResume.objects.for_user(user).filter(is_library=True, track=track).order_by("-uploaded_at").first()
            or latest_overall
        )

    cooldown_before = timezone.now() - timedelta(hours=VETTING_MATCHING_RETRY_COOLDOWN_HOURS)
    to_enqueue: list[int] = []
    for e in entries:
        latest = latest_by_track.get(e.track)
        if not latest:
            continue
        resume_outdated = e.vetting_interview_resume_id != latest.id
        missing_prob = e.vetting_interview_probability is None
        if not missing_prob and not resume_outdated:
            continue
        if missing_prob and not resume_outdated:
            scored_at = e.vetting_interview_scored_at
            if scored_at and scored_at > cooldown_before:
                continue
        to_enqueue.append(e.id)
        if len(to_enqueue) >= max_to_enqueue:
            break

    if to_enqueue:
        evaluate_vetting_matching_task(user.id, to_enqueue, llm_provider=None, llm_model=None, matching_prompt=None)

    apply_vetting_to_applying_promotions(user)


@db_periodic_task(crontab(minute="*/20"))
def enqueue_due_vetting_matching_tasks():
    """
    Periodic dispatcher (every 20 mins): Fans out vetting evaluation tasks per active user
    so background Huey workers process them concurrently.
    """
    User = get_user_model()
    active_user_ids = list(User.objects.filter(is_active=True).values_list("id", flat=True))
    for uid in active_user_ids:
        process_user_vetting_matching_task(uid)
    return {"status": "dispatched", "users_count": len(active_user_ids)}


def _pipeline_stage_filter() -> models.Q:
    return models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)


def _cleanup_retention_stage_q(stage_key: str) -> models.Q:
    """Single pipeline board stage for retention purge (not 'all')."""
    s = (stage_key or "").strip().lower()
    if s == "pipeline":
        return models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)
    if s == "vetting":
        return models.Q(stage=PipelineEntry.Stage.VETTING)
    if s == "applying":
        return models.Q(stage=PipelineEntry.Stage.APPLYING)
    if s == "done":
        return models.Q(stage=PipelineEntry.Stage.DONE)
    raise ValueError(f"Invalid retention stage: {stage_key!r}")


def apply_cleanup_retention_purge(cfg: AppAutomationSettings) -> int:
    """
    Remove pipeline rows older than tenant retention policy (default 2 weeks / 14 days; configurable per tenant).
    Excludes Applied (Done) stage jobs unless cleanup_done_retention_days is explicitly configured.
    Same removal policy: hard-delete unless liked/disliked (which are soft-deleted with mark_deleted).
    """
    now = timezone.now()
    user = cfg.owner
    from .search_profile_scope import profile_slugs_for_pipeline

    track_slugs = profile_slugs_for_pipeline(user)
    if not track_slugs:
        return 0

    general_days = int(getattr(cfg, "cleanup_job_retention_days", 14) or 14)

    rules: list[tuple[str, int]] = [
        ("pipeline", int(cfg.cleanup_pipeline_retention_days) if cfg.cleanup_pipeline_retention_days else general_days),
        ("vetting", int(cfg.cleanup_vetting_retention_days) if cfg.cleanup_vetting_retention_days else general_days),
        ("applying", int(cfg.cleanup_applying_retention_days) if cfg.cleanup_applying_retention_days else general_days),
        ("done", int(cfg.cleanup_done_retention_days or 0)),
    ]

    removed = 0
    for tslug in track_slugs:
        for stage_key, days in rules:
            if days < 1:
                continue
            cutoff = now - timedelta(days=days)
            try:
                st_q = _cleanup_retention_stage_q(stage_key)
            except ValueError:
                continue
            for entry in PipelineEntry.objects.for_user(user).filter(
                track=tslug,
                removed_at__isnull=True,
                added_at__lt=cutoff,
            ).filter(st_q):
                _pipeline_entry_remove_for_cleanup(entry)
                removed += 1
    if removed:
        logger.info("[cleanup_manager] retention purge removed=%d row(s)", removed)
        huey_logger.info("[cleanup_manager] retention purge removed=%d row(s)", removed)
    return removed


def _iter_pipeline_stage_job_listing_ids(user, track: str) -> list[int]:
    return sorted(
        set(
            PipelineEntry.objects.for_user(user).filter(track=track, removed_at__isnull=True)
            .filter(_pipeline_stage_filter())
            .values_list("job_listing_id", flat=True)
        )
    )


def _job_listing_has_like_or_dislike(job_listing_id: int) -> bool:
    return JobListingAction.objects.filter(
        job_listing_id=job_listing_id,
        action__in=[
            JobListingAction.ActionType.LIKED,
            JobListingAction.ActionType.DISLIKED,
        ],
    ).exists()


def _pipeline_entry_remove_for_cleanup(entry: PipelineEntry) -> None:
    if _job_listing_has_like_or_dislike(entry.job_listing_id):
        entry.mark_deleted(save=True)
    else:
        entry.delete()


PIPELINE_MANAGER_STATS_MAX_AGE_DAYS = 2
PIPELINE_MANAGER_PURGE_MARGIN_MAX = -2
PIPELINE_MANAGER_BATCH_SIZE = 100


@db_task()
def process_user_pipeline_manager_task(user_id: int):
    """
    Huey worker task: Run pipeline scoring, purging, and auto-promotions for a single user.
    Enables distributed, parallel pipeline maintenance across Huey workers.
    """
    try:
        user = _task_user(user_id)
        return _pipeline_manager_for_user(user)
    except Exception as e:
        logger.exception("[process_user_pipeline_manager_task] failed for user_id=%s: %s", user_id, e)
        return {"status": "error", "message": str(e), "user_id": user_id}


@db_periodic_task(crontab(minute="*/30"))
def pipeline_manager():
    """
    Periodic dispatcher (every 30 mins): Fans out pipeline maintenance tasks
    to individual Huey worker tasks per active user for horizontal scalability.
    """
    User = get_user_model()
    active_user_ids = list(User.objects.filter(is_active=True).values_list("id", flat=True))
    for uid in active_user_ids:
        process_user_pipeline_manager_task(uid)
    logger.info("[pipeline_manager] Dispatched pipeline maintenance for %d active user(s)", len(active_user_ids))
    return {"status": "dispatched", "users_count": len(active_user_ids)}


def _pipeline_manager_for_user(user):
    from .search_profile_scope import profile_slugs_for_pipeline

    track_slugs = profile_slugs_for_pipeline(user)
    if not track_slugs:
        return None
    now = timezone.now()
    stats_cutoff = now - timedelta(days=PIPELINE_MANAGER_STATS_MAX_AGE_DAYS)
    stage_q = _pipeline_stage_filter()

    for track in track_slugs:
        try:
            job_ids = _iter_pipeline_stage_job_listing_ids(user, track)
            if not job_ids:
                continue

            existing_metrics = {
                m.job_listing_id: m.last_scored_at
                for m in JobListingTrackMetrics.objects.for_user(user).filter(
                    track=track, job_listing_id__in=job_ids
                ).only("job_listing_id", "last_scored_at")
            }
            needs_scoring = [
                jid
                for jid in job_ids
                if existing_metrics.get(jid) is None or existing_metrics[jid] < stats_cutoff
            ]
            if needs_scoring:
                logger.info(
                    "[pipeline_manager] track=%s jobs_to_score=%d",
                    track,
                    len(needs_scoring),
                )
            for i in range(0, len(needs_scoring), PIPELINE_MANAGER_BATCH_SIZE):
                batch_ids = needs_scoring[i : i + PIPELINE_MANAGER_BATCH_SIZE]
                jobs = list(JobListing.objects.filter(id__in=batch_ids))
                if not jobs:
                    continue
                persist_preference_metrics_for_jobs(
                    user=user,
                    track=track,
                    job_listings=jobs,
                )

            bad_ids = list(
                JobListingTrackMetrics.objects.for_user(user).filter(
                    track=track,
                    preference_margin__lt=PIPELINE_MANAGER_PURGE_MARGIN_MAX,
                ).values_list("job_listing_id", flat=True)
            )
            if bad_ids:
                margin_entries = PipelineEntry.objects.for_user(user).filter(
                    track=track,
                    removed_at__isnull=True,
                    job_listing_id__in=bad_ids,
                ).filter(stage_q)
                removed_n = 0
                for entry in margin_entries:
                    _pipeline_entry_remove_for_cleanup(entry)
                    removed_n += 1
                if removed_n:
                    logger.info(
                        "[pipeline_manager] track=%s purged %d job(s) from pipeline (margin)",
                        track,
                        removed_n,
                    )
        except Exception as e:
            logger.exception("[pipeline_manager] track=%s failed: %s", track, e)
    try:
        apply_pipeline_auto_promotions(user)
    except Exception as e:
        logger.exception("[pipeline_manager] apply_pipeline_auto_promotions failed: %s", e)
    return None


MAX_DUE_JOB_SEARCH_TASKS_PER_TICK = 50


@db_periodic_task(crontab(minute="*"))
def enqueue_due_job_search_tasks():
    """
    Runs every minute: find JobSearchTasks across all users where next_run_at <= now,
    enqueues individual run_job_search_task runs (isolated by per-user locks),
    and updates next_run_at to the next occurrence.
    """
    now = timezone.now()
    due = list(
        JobSearchTask.objects.filter(is_active=True, next_run_at__isnull=False)
        .filter(next_run_at__lte=now)
        .order_by("next_run_at", "start_time")[:MAX_DUE_JOB_SEARCH_TASKS_PER_TICK]
    )
    enqueued_count = 0
    for task in due:
        try:
            next_run = get_next_run_at(task.frequency, from_time=now)
            task.next_run_at = next_run
            task.save(update_fields=["next_run_at", "updated_at"])
            run_job_search_task(task.owner_id, task.id)
            enqueued_count += 1
            logger.info(
                "[enqueue_due_job_search_tasks] enqueued task_id=%s owner_id=%s next_run_at=%s",
                task.id,
                task.owner_id,
                next_run,
            )
        except Exception as e:
            logger.exception("[enqueue_due_job_search_tasks] task_id=%s failed: %s", task.id, e)
    return {"status": "ok", "enqueued": enqueued_count}


# Runs stuck in RUNNING longer than this are marked FAILED (worker likely died or fetch hung)
JOB_SEARCH_RUN_STALE_MINUTES = 60


@db_periodic_task(crontab(minute="*/15"))
def mark_stale_job_search_runs_failed():
    """
    Mark JobSearchTaskRun rows that have been RUNNING longer than JOB_SEARCH_RUN_STALE_MINUTES
    as FAILED. Jobs are only added when a run reaches COMPLETED; stuck RUNNING runs mean
    the worker never finished (e.g. fetch hung or worker restarted).
    """
    threshold = timezone.now() - timedelta(minutes=JOB_SEARCH_RUN_STALE_MINUTES)
    stale = JobSearchTaskRun.objects.filter(
        status=JobSearchTaskRun.STATUS_RUNNING,
        started_at__lt=threshold,
    )
    count = stale.update(
        status=JobSearchTaskRun.STATUS_FAILED,
        finished_at=timezone.now(),
        error_message="Run timed out (marked stale after {} minutes). Worker may have died or job fetch hung.".format(
            JOB_SEARCH_RUN_STALE_MINUTES
        ),
    )
    if count:
        logger.info("[mark_stale_job_search_runs_failed] marked %d run(s) as failed", count)
    return None


@db_task()
def process_user_cleanup_task(user_id: int):
    """
    Huey worker task: Run deduplication and retention purges for a single user.
    Enables distributed, parallel cleanup processing across Huey workers.
    """
    try:
        user = _task_user(user_id)
        cfg = AppAutomationSettings.get_for_user(user)
        from .job_dedupe import dedupe_pipeline_entries

        dedupe_res = dedupe_pipeline_entries(
            user=user,
            track_slug="*",
            stage="all",
            include_done=False,
        )
        retention_res = apply_cleanup_retention_purge(cfg)
        from .resume_cleanup import purge_generated_user_resumes

        resumes_res = purge_generated_user_resumes(
            int(cfg.cleanup_generated_resume_retention_days or 0),
            user=user,
        )
        return {
            "user_id": user_id,
            "dedupe": dedupe_res,
            "retention_removed": retention_res,
            "resumes_removed": resumes_res,
        }
    except Exception as e:
        logger.exception("[process_user_cleanup_task] failed for user_id=%s: %s", user_id, e)
        return {"user_id": user_id, "error": str(e)}


@db_task()
def purge_inactive_pipeline_entries_task(limit: int = 400):
    """
    Huey worker task: Best-effort inactive URL check for Applying listings.
    """
    try:
        from .job_activity import purge_inactive_pipeline_entries

        return purge_inactive_pipeline_entries(limit=limit)
    except Exception as e:
        logger.exception("[purge_inactive_pipeline_entries_task] failed: %s", e)
        return {"error": str(e)}


@db_periodic_task(crontab(minute="30", hour="1"))
def cleanup_manager():
    """
    Once daily (01:30): Fan out user-scoped cross-profile dedupe and 14-day retention purges across
    Huey workers for scalability.
    """
    started_at = timezone.now()
    User = get_user_model()
    active_user_ids = list(User.objects.filter(is_active=True).values_list("id", flat=True))
    for uid in active_user_ids:
        process_user_cleanup_task(uid)

    payload = {
        "status": "dispatched",
        "started_at": started_at,
        "finished_at": timezone.now(),
        "users_dispatched": len(active_user_ids),
    }
    cache.set(CLEANUP_STATUS_CACHE_KEY, payload, CLEANUP_STATUS_CACHE_TTL_SECONDS)
    logger.info("[cleanup_manager] Dispatched daily cleanup for %d active user(s)", len(active_user_ids))
    return {"status": "dispatched", "users_dispatched": len(active_user_ids)}


@db_task()
def process_user_purge_generated_resumes_task(user_id: int):
    """
    Huey worker task: Remove old optimizer ephemeral UserResume PDFs for a single user.
    """
    try:
        user = _task_user(user_id)
        cfg = AppAutomationSettings.get_for_user(user)
        from .resume_cleanup import purge_generated_user_resumes

        purge_generated_user_resumes(
            int(cfg.cleanup_generated_resume_retention_days or 0),
            user=user,
        )
    except Exception as e:
        logger.exception("[process_user_purge_generated_resumes_task] failed for user_id=%s: %s", user_id, e)


@db_periodic_task(crontab(minute="15", hour="*/6"))
def purge_generated_resumes_periodic():
    """Every 6 hours: fan-out removal of old optimizer ephemeral UserResume PDFs per user."""
    User = get_user_model()
    active_user_ids = list(User.objects.filter(is_active=True).values_list("id", flat=True))
    for uid in active_user_ids:
        process_user_purge_generated_resumes_task(uid)
    return {"status": "dispatched", "users_count": len(active_user_ids)}


# Backward-compatible alias (imports, docs, manual enqueue).
cleanup_inactive_pipeline_entries_daily = cleanup_manager


@db_task()
def dedupe_pipeline_jobs_task(
    track: str = "*",
    stage: str = "all",
    include_done: bool = False,
):
    """
    Huey: remove duplicate pipeline rows (same title/company/description, different location).
    track: '*' or 'all' for every track; else a single track slug.
    stage: 'all' for pipeline+vetting+applying (+ done if include_done); or one of pipeline/vetting/applying/done.
    Runs across all active users (staff/admin task only).
    """
    from .job_dedupe import dedupe_pipeline_entries_all_users

    try:
        return dedupe_pipeline_entries_all_users(
            track_slug=track,
            stage=stage,
            include_done=include_done,
        )
    except ValueError as e:
        logger.warning("[dedupe_pipeline_jobs_task] invalid params: %s", e)
        return {"status": "error", "message": str(e)}


@db_task()
def pipeline_resume_llm_extract_task(run_dir_str: str):
    """
    Huey: batched OpenAI skill extraction for pipeline resume summary (see pipeline_llm_skill_extract).
    """
    from pathlib import Path

    from django.conf import settings
    from django.contrib.auth import get_user_model

    from .pipeline_llm_skill_extract import (
        effective_pipeline_batch_size,
        fetch_jobs_ordered,
        load_run_meta,
        resolve_provider_api_key,
        run_pipeline_llm_extraction,
        write_run_error,
    )

    run_dir = Path(run_dir_str)
    try:
        meta = load_run_meta(run_dir)
    except Exception as e:
        logger.exception("[pipeline_resume_llm_extract_task] invalid run dir %s", run_dir_str)
        return {"status": "error", "message": str(e)}

    owner_id = meta.get("owner_id")
    if not owner_id:
        write_run_error(run_dir, "Run metadata missing owner_id.")
        return {"status": "error", "message": "missing_owner"}
    User = get_user_model()
    try:
        user = User.objects.get(pk=int(owner_id))
    except (TypeError, ValueError, User.DoesNotExist):
        write_run_error(run_dir, f"Owner {owner_id!r} not found.")
        return {"status": "error", "message": "owner_not_found"}

    provider = (meta.get("provider") or "OpenAI").strip()
    api_key = resolve_provider_api_key(provider, user=user)
    if not api_key:
        write_run_error(
            run_dir,
            f"No API key available for provider {provider!r}. Connect in LLM settings or configure env.",
        )
        return {"status": "error", "message": "missing_api_key"}

    jobs = fetch_jobs_ordered(meta["entry_ids"])
    model = (meta.get("model") or "").strip()
    if not model:
        write_run_error(run_dir, "Run metadata missing model.")
        return {"status": "error", "message": "missing_model"}

    try:
        run_pipeline_llm_extraction(
            run_dir,
            user=user,
            provider=provider,
            api_key=api_key,
            jobs=jobs,
            model=model,
            batch_size=effective_pipeline_batch_size(provider),
            max_tokens_per_minute=max(0, int(getattr(settings, "PIPELINE_LLM_MAX_TOKENS_PER_MINUTE", 90000))),
            requests_per_minute=max(0, int(getattr(settings, "PIPELINE_LLM_REQUESTS_PER_MINUTE", 60))),
            http_max_attempts=max(1, int(getattr(settings, "PIPELINE_LLM_HTTP_MAX_ATTEMPTS", 3))),
        )
    except Exception as e:
        logger.exception("[pipeline_resume_llm_extract_task] failed: %s", e)
        write_run_error(run_dir, str(e))
        return {"status": "error", "message": str(e)}

    return {"status": "ok", "run_dir": run_dir_str}

