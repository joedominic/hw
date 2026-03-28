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
from .job_sources import DEFAULT_SITE_NAMES
from .job_search_core import run_job_search_core, recompute_preferences_for_jobs
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
from .llm_gateway import USAGE_QUERY_PIPELINE_VETTING
from .llm_services import is_auth_error
from .llm_session import get_runtime_provider_candidates, get_active_llm_provider
from .prompt_store import (
    build_optimizer_graph_prompt_state,
    profile_for_llm,
    resolve_prompt_parts,
)
import time
import logging

logger = logging.getLogger(__name__)
huey_logger = logging.getLogger("huey")


#
# Vetting matching (resume vs job) evaluation
#
VETTING_MATCHING_LOCK_KEY = "vetting_matching_task_running"
VETTING_MATCHING_LOCK_TIMEOUT = 3600  # 1 hour max
RESUME_MATCHING_SNIPPET_CHARS = 8000
VETTING_MATCHING_JD_MIN_CHARS = 2000


def apply_vetting_to_applying_promotions(entry_ids: list[int] | None = None) -> int:
    """
    Move VETTING entries to APPLYING when interview probability >= configured minimum.
    If entry_ids is set, only those ids are considered (still must match stage/score rules).

    Does not enqueue resume optimization (manual Optimize on the Applying board only).
    """
    cfg = AppAutomationSettings.get_solo()
    if not cfg.vetting_to_applying_enabled:
        return 0
    y = int(cfg.vetting_interview_probability_min)
    qs = PipelineEntry.objects.filter(
        stage=PipelineEntry.Stage.VETTING,
        removed_at__isnull=True,
        vetting_interview_probability__isnull=False,
        vetting_interview_probability__gte=y,
    )
    if entry_ids is not None:
        qs = qs.filter(id__in=entry_ids)
    n = 0
    for entry in qs:
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


# Lock key so only one job-search task runs at a time (avoids overlap)
JOB_SEARCH_TASK_LOCK_KEY = "job_search_task_running"
JOB_SEARCH_TASK_LOCK_TIMEOUT = 3600  # 1 hour max
CLEANUP_STATUS_CACHE_KEY = "cleanup_inactive_pipeline_entries_last_status"
CLEANUP_STATUS_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _create_agent_log(optimized_resume, steps, prev_node, accumulated):
    """Create one AgentLog for the completed node; resolve step_display from steps if step_*."""
    step_display = prev_node
    if steps and prev_node and prev_node.startswith("step_"):
        try:
            idx = int(prev_node.split("_", 1)[1])
            if 0 <= idx < len(steps):
                step_display = steps[idx]
        except (ValueError, IndexError):
            pass
    AgentLog.objects.create(
        optimized_resume=optimized_resume,
        step_name=step_display,
        thought=accumulated.get(prev_node) or {},
    )


@db_task()
def optimize_resume_task(
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
):
    """
    Huey async task: run the resume optimizer workflow for a single OptimizedResume.

    Fetches OptimizedResume once at top; on any failure sets status to failed.
    """
    try:
        optimized_resume = OptimizedResume.objects.get(id=resume_id)
    except OptimizedResume.DoesNotExist:
        return {"status": "error", "message": "OptimizedResume not found"}

    try:
        job_desc = optimized_resume.job_description.content

        # Parse PDF
        resume_text = parse_pdf(optimized_resume.original_resume.file.path)

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
        _pb = build_optimizer_graph_prompt_state(prompts or None)
        initial_state = {
            "resume_text": resume_text,
            "source_resume_text": resume_text,
            "job_description": job_desc,
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
        }

        # Run Graph
        last_state = initial_state
        optimized_resume.status = OptimizedResume.STATUS_RUNNING
        optimized_resume.save(update_fields=["status"])
        usage_callback = TokenUsageCallback()
        # recursion_limit must exceed the number of nodes: LangGraph counts each node invocation
        # and the transition to END; with limit=num_steps the last node can hit the limit before finishing.
        num_steps = len(steps) if steps else 3
        run_config = {"callbacks": [usage_callback], "recursion_limit": num_steps + 20}
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

                if 'ats_score' in state_update or 'recruiter_score' in state_update:
                    optimized_resume.status_display = f"Scoring: ATS={last_state.get('ats_score', 'N/A')}, Recruiter={last_state.get('recruiter_score', 'N/A')}"
                elif 'optimized_resume' in state_update:
                    optimized_resume.status_display = "Drafting"

                optimized_resume.save(update_fields=["status_display"])

                if rate_limit_delay and float(rate_limit_delay) > 0:
                    time.sleep(float(rate_limit_delay))
        if prev_node is not None:
            _create_agent_log(optimized_resume, steps, prev_node, accumulated)

        optimized_resume.refresh_from_db()
        if optimized_resume.status == OptimizedResume.STATUS_FAILED and (optimized_resume.error_message or "").strip() == CANCELLED_MESSAGE:
            return {"status": "cancelled", "resume_id": resume_id}

        # Final update
        optimized_resume.optimized_content = last_state['optimized_resume']
        optimized_resume.status = OptimizedResume.STATUS_COMPLETED
        optimized_resume.ats_score = last_state.get('ats_score')
        optimized_resume.recruiter_score = last_state.get('recruiter_score')
        optimized_resume.status_display = ""
        optimized_resume.total_input_tokens = usage_callback.total_input_tokens or None
        optimized_resume.total_output_tokens = usage_callback.total_output_tokens or None
        optimized_resume.save()

        return {"status": "success", "resume_id": resume_id}

    except Exception as e:
        if is_auth_error(e):
            LLMProviderConfig.objects.filter(provider=provider).update(encrypted_api_key="", last_validated_at=None)
        optimized_resume.refresh_from_db()
        optimized_resume.status = OptimizedResume.STATUS_FAILED
        optimized_resume.error_message = str(e)
        optimized_resume.save()
        return {"status": "error", "message": str(e)}


PIPELINE_OPT_MIN_JD_CHARS = 50


def _build_pipeline_job_description(job: JobListing) -> str:
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
    desc = (job.description or "").strip()
    url = (job.url or "").strip()
    tail_parts = [p for p in (desc, f"URL: {url}" if url else "") if p]
    tail = "\n\n".join(tail_parts)
    if header and tail:
        return f"{header}\n\n{tail}".strip()
    return (header or tail).strip()


def _resolve_user_resume_for_track(track_slug: str) -> UserResume | None:
    track_slug = (track_slug or "").strip().lower()
    latest_track = (
        UserResume.objects.filter(track=track_slug).order_by("-uploaded_at").first()
        if track_slug
        else None
    )
    if latest_track and latest_track.file:
        return latest_track
    latest = UserResume.objects.order_by("-uploaded_at").first()
    if latest and latest.file:
        return latest
    return None


def _resolve_llm_for_pipeline_optimization() -> tuple[str | None, LLMProviderConfig | None]:
    provider = get_active_llm_provider(None)
    config = None
    if provider:
        config = (
            LLMProviderConfig.objects.filter(provider=provider)
            .exclude(encrypted_api_key="")
            .first()
        )
    if not config:
        cands = get_runtime_provider_candidates()
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

    if not force_new:
        active = OptimizedResume.objects.filter(
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
    jd_text = _build_pipeline_job_description(job)
    if len(jd_text) < PIPELINE_OPT_MIN_JD_CHARS:
        return {
            "status": "error",
            "message": f"Job description too short (min {PIPELINE_OPT_MIN_JD_CHARS} chars)",
            "entry_id": pipeline_entry_id,
        }

    user_resume = _resolve_user_resume_for_track(entry.track)
    if not user_resume or not user_resume.file:
        return {
            "status": "error",
            "message": "No resume PDF for this track (upload in Optimizer)",
            "entry_id": pipeline_entry_id,
        }

    provider, config = _resolve_llm_for_pipeline_optimization()
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

    solo = AppAutomationSettings.get_solo()
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

    jd = JobDescription.objects.create(content=jd_text)
    opt = OptimizedResume.objects.create(
        original_resume=user_resume,
        job_description=jd,
        status=OptimizedResume.STATUS_QUEUED,
        pipeline_entry=entry,
        optimizer_workflow=workflow,
    )

    optimize_resume_task(
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
    )
    return {
        "status": "ok",
        "entry_id": pipeline_entry_id,
        "optimized_resume_id": opt.id,
    }


@db_task()
def enqueue_applying_resume_optimization_task(
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
def run_job_search_task(task_id):
    """
    Huey task: run one JobSearchTask (fetch, filter, rank, add to pipeline).
    Uses a global lock so only one job-search task runs at a time.
    """
    if not cache.add(JOB_SEARCH_TASK_LOCK_KEY, 1, JOB_SEARCH_TASK_LOCK_TIMEOUT):
        logger.warning("[run_job_search_task] Skipping task %s: another job search task is running", task_id)
        return {"status": "skipped", "message": "Another job search task is running"}
    try:
        return _run_job_search_task_impl(task_id)
    finally:
        cache.delete(JOB_SEARCH_TASK_LOCK_KEY)


def _run_job_search_task_impl(task_id):
    try:
        task = JobSearchTask.objects.get(id=task_id)
    except JobSearchTask.DoesNotExist:
        return {"status": "error", "message": "JobSearchTask not found"}
    if not task.is_active:
        return {"status": "skipped", "message": "Task is inactive"}

    run = JobSearchTaskRun.objects.create(
        task=task,
        status=JobSearchTaskRun.STATUS_RUNNING,
    )
    try:
        jobs_fetched, jobs_after_filter, jobs_out, _refs = run_job_search_core(
            search_term=task.search_term,
            location=task.location or None,
            track=task.track or None,
            results_wanted=task.jobs_to_fetch,
            site_name=task.site_name if isinstance(task.site_name, list) else list(DEFAULT_SITE_NAMES),
        )
    except Exception as e:
        logger.exception("[run_job_search_task] task_id=%s core failed: %s", task_id, e)
        run.status = JobSearchTaskRun.STATUS_FAILED
        run.finished_at = timezone.now()
        run.error_message = str(e)
        run.save()
        return {"status": "error", "message": str(e)}

    jobs_added_to_pipeline = 0
    for payload in jobs_out:
        job_id = payload.id
        pe = PipelineEntry.objects.filter(job_listing_id=job_id, track=task.track).first()
        if pe is None:
            PipelineEntry.objects.create(
                job_listing_id=job_id,
                track=task.track,
                stage=PipelineEntry.Stage.PIPELINE,
            )
            jobs_added_to_pipeline += 1
        elif pe.removed_at is not None:
            pass  # user soft-deleted; do not re-add

    try:
        from .job_dedupe import dedupe_pipeline_entries

        dedupe_result = dedupe_pipeline_entries(
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

    run.jobs_fetched = jobs_fetched
    run.jobs_after_filter = jobs_after_filter
    run.jobs_added_to_pipeline = jobs_added_to_pipeline
    run.status = JobSearchTaskRun.STATUS_COMPLETED
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "[run_job_search_task] task_id=%s done: fetched=%d after_filter=%d added=%d",
        task_id, jobs_fetched, jobs_after_filter, jobs_added_to_pipeline,
    )
    return {"status": "success", "task_id": task_id}


@db_task()
def evaluate_vetting_matching_task(
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

    if not cache.add(VETTING_MATCHING_LOCK_KEY, 1, VETTING_MATCHING_LOCK_TIMEOUT):
        logger.warning(
            "[evaluate_vetting_matching_task] Skipping: another vetting matching task is running"
        )
        return {"status": "skipped", "message": "Another vetting matching task is running"}

    try:
        entries = list(
            PipelineEntry.objects.filter(
                id__in=pipeline_entry_ids,
                stage=PipelineEntry.Stage.VETTING,
                removed_at__isnull=True,
            )
            .select_related("job_listing")
        )
        if not entries:
            return {"status": "skipped", "message": "No entries in VETTING stage"}

        from .llm_gateway import preference_candidates_available

        if not preference_candidates_available():
            return {"status": "error", "message": "No LLM configured (add provider keys and preference rows)."}

        jd_max_chars = getattr(settings, "JOB_MATCHING_JD_MAX_CHARS", 12000)
        now = timezone.now()
        resume_snippet_map: dict[str, tuple[UserResume, str]] = {}

        # Parse each track's resume once per task.
        tracks = {e.track for e in entries if e.track}
        latest_overall = UserResume.objects.order_by("-uploaded_at").first()
        for track in tracks:
            latest = UserResume.objects.filter(track=track).order_by("-uploaded_at").first() or latest_overall
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

            jd = (entry.job_listing.description or "").strip()
            if not jd or len(jd) < VETTING_MATCHING_JD_MIN_CHARS:
                skipped += 1
                continue
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
                            None,
                            prompt_template=matching_prompt,
                            job_cache_key=f"vetting:{entry.id}",
                            usage_query_kind=USAGE_QUERY_PIPELINE_VETTING,
                        )
                    else:
                        result = run_matching(
                            resume_snippet,
                            jd,
                            None,
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
                try:
                    ip_int = int(ip) if ip is not None else None
                    if ip_int is not None:
                        ip_int = max(0, min(100, ip_int))
                except (TypeError, ValueError):
                    ip_int = None

                entry.vetting_interview_probability = ip_int
                entry.vetting_interview_reasoning = reasoning[:2000] if reasoning else ""
                entry.vetting_interview_resume_id = resume_obj.id
                entry.vetting_interview_scored_at = now
                entry.save(
                    update_fields=[
                        "vetting_interview_probability",
                        "vetting_interview_reasoning",
                        "vetting_interview_resume_id",
                        "vetting_interview_scored_at",
                    ]
                )
                updated += 1
                apply_vetting_to_applying_promotions([entry.id])
            except Exception as e:
                logger.exception("[evaluate_vetting_matching_task] entry_id=%s failed: %s", entry.id, e)
                errors.append(f"Entry {entry.id}: {e}")

        return {
            "status": "success",
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:20],
        }
    finally:
        cache.delete(VETTING_MATCHING_LOCK_KEY)


def apply_pipeline_auto_promotions() -> int:
    """
    Promote PIPELINE (or legacy blank stage) entries to VETTING when per-track Pref margin
    meets the configured minimum; enqueue vetting matching for promoted rows.
    """
    cfg = AppAutomationSettings.get_solo()
    if not cfg.pipeline_to_vetting_enabled:
        return 0
    min_margin = cfg.pipeline_preference_margin_min
    entries = (
        PipelineEntry.objects.filter(removed_at__isnull=True)
        .filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE))
        .only("id", "job_listing_id", "track")
    )
    promoted: list[int] = []
    for entry in entries:
        m = (
            JobListingTrackMetrics.objects.filter(
                job_listing_id=entry.job_listing_id,
                track=entry.track,
            )
            .only("preference_margin")
            .first()
        )
        if not m or m.preference_margin is None:
            continue
        if m.preference_margin < min_margin:
            continue
        entry.move_to_vetting(save=True)
        promoted.append(entry.id)
    if promoted:
        evaluate_vetting_matching_task(
            promoted, llm_provider=None, llm_model=None, matching_prompt=None
        )
    return len(promoted)


@db_periodic_task(crontab(minute="*/20"))
def enqueue_due_vetting_matching_tasks():
    """
    Periodically backfill vetting interview probability for entries missing it.
    Keeps existing rows correct when the "latest" resume changes per track.
    """
    # Limit work each tick; matching calls are expensive.
    max_to_enqueue = 20
    candidate_entries = (
        PipelineEntry.objects.filter(
            stage=PipelineEntry.Stage.VETTING,
            removed_at__isnull=True,
        )
        .order_by("-added_at")[:200]
        .only(
            "id",
            "track",
            "vetting_interview_probability",
            "vetting_interview_resume_id",
        )
    )

    entries = list(candidate_entries)
    if not entries:
        return None

    # Resolve latest resume id per track.
    latest_overall = UserResume.objects.order_by("-uploaded_at").first()
    latest_by_track: dict[str, UserResume | None] = {}
    for track in {e.track for e in entries if e.track}:
        latest_by_track[track] = UserResume.objects.filter(track=track).order_by("-uploaded_at").first() or latest_overall

    to_enqueue: list[int] = []
    for e in entries:
        latest = latest_by_track.get(e.track)
        if not latest:
            continue
        if (
            e.vetting_interview_probability is None
            or e.vetting_interview_resume_id != latest.id
        ):
            to_enqueue.append(e.id)
        if len(to_enqueue) >= max_to_enqueue:
            break

    if to_enqueue:
        evaluate_vetting_matching_task(to_enqueue, llm_provider=None, llm_model=None, matching_prompt=None)

    apply_vetting_to_applying_promotions()

    return None


def _pipeline_stage_filter() -> models.Q:
    return models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)


def _iter_pipeline_stage_job_listing_ids(track: str) -> list[int]:
    return sorted(
        set(
            PipelineEntry.objects.filter(track=track, removed_at__isnull=True)
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
PIPELINE_MANAGER_ENTRY_MAX_AGE_DAYS = 5
PIPELINE_MANAGER_PURGE_MARGIN_MAX = -2
PIPELINE_MANAGER_BATCH_SIZE = 100


@db_periodic_task(crontab(minute="*/30"))
def pipeline_manager():
    """
    Every 30 minutes: maintain Pipeline-stage entries only—age purge, refresh stale/missing
    fit metrics, purge low preference_margin rows, then auto-promote to Vetting when enabled.
    """
    track_slugs = list(Track.objects.values_list("slug", flat=True))
    if not track_slugs:
        return None
    now = timezone.now()
    stats_cutoff = now - timedelta(days=PIPELINE_MANAGER_STATS_MAX_AGE_DAYS)
    entry_age_cutoff = now - timedelta(days=PIPELINE_MANAGER_ENTRY_MAX_AGE_DAYS)
    stage_q = _pipeline_stage_filter()

    for track in track_slugs:
        try:
            old_entries = PipelineEntry.objects.filter(
                track=track,
                removed_at__isnull=True,
                added_at__lt=entry_age_cutoff,
            ).filter(stage_q)
            for entry in old_entries:
                _pipeline_entry_remove_for_cleanup(entry)

            job_ids = _iter_pipeline_stage_job_listing_ids(track)
            if not job_ids:
                continue

            existing_metrics = {
                m.job_listing_id: m.last_scored_at
                for m in JobListingTrackMetrics.objects.filter(
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
                scores = recompute_preferences_for_jobs(jobs, track=track)
                for job in jobs:
                    data = scores.get(job.id) or {}
                    JobListingTrackMetrics.objects.update_or_create(
                        job_listing=job,
                        track=track,
                        defaults={
                            "focus_percent": data.get("focus_percent"),
                            "focus_after_penalty": data.get("focus_after_penalty"),
                            "preference_margin": data.get("preference_margin"),
                            "last_scored_at": timezone.now(),
                        },
                    )

            bad_ids = list(
                JobListingTrackMetrics.objects.filter(
                    track=track,
                    preference_margin__lt=PIPELINE_MANAGER_PURGE_MARGIN_MAX,
                ).values_list("job_listing_id", flat=True)
            )
            if bad_ids:
                margin_entries = PipelineEntry.objects.filter(
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
        apply_pipeline_auto_promotions()
    except Exception as e:
        logger.exception("[pipeline_manager] apply_pipeline_auto_promotions failed: %s", e)
    return None


@db_periodic_task(crontab(minute="*"))
def enqueue_due_job_search_tasks():
    """
    Runs every minute: find JobSearchTasks where next_run_at <= now, enqueue one,
    then update next_run_at to the next occurrence. Only one task enqueued per tick
    to avoid overlap (run_job_search_task also uses a lock).
    """
    now = timezone.now()
    due = (
        JobSearchTask.objects.filter(is_active=True, next_run_at__isnull=False)
        .filter(next_run_at__lte=now)
        .order_by("next_run_at", "start_time")[:1]
    )
    for task in due:
        try:
            next_run = get_next_run_at(task.frequency, from_time=now)
            task.next_run_at = next_run
            task.save(update_fields=["next_run_at", "updated_at"])
            run_job_search_task(task.id)
            logger.info("[enqueue_due_job_search_tasks] enqueued task_id=%s next_run_at=%s", task.id, next_run)
        except Exception as e:
            logger.exception("[enqueue_due_job_search_tasks] task_id=%s failed: %s", task.id, e)
    return None


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


@db_periodic_task(crontab(minute="30", hour="1"))
def cleanup_inactive_pipeline_entries_daily():
    """
    Once daily: remove inactive/closed postings from Applying stage, then run dedupe.
    """
    started_at = timezone.now()
    purge_result = {"checked": 0, "removed_inactive": 0, "active": 0, "unknown": 0}
    dedupe_result = {"entries_removed": 0, "duplicate_groups": 0}
    status = "success"
    errors: list[str] = []
    try:
        from .job_activity import purge_inactive_pipeline_entries

        purge_result = purge_inactive_pipeline_entries(limit=400)
        huey_logger.info(
            "[cleanup_inactive_pipeline_entries_daily] inactive-check done checked=%s removed_inactive=%s active=%s unknown=%s",
            purge_result.get("checked"),
            purge_result.get("removed_inactive"),
            purge_result.get("active"),
            purge_result.get("unknown"),
        )
        logger.info(
            "[cleanup_inactive_pipeline_entries_daily] checked=%s removed_inactive=%s active=%s unknown=%s",
            purge_result.get("checked"),
            purge_result.get("removed_inactive"),
            purge_result.get("active"),
            purge_result.get("unknown"),
        )
    except Exception as e:
        status = "partial_failure"
        errors.append(f"inactive_cleanup: {e}")
        logger.exception("[cleanup_inactive_pipeline_entries_daily] inactive cleanup failed: %s", e)

    try:
        from .job_dedupe import dedupe_pipeline_entries

        dedupe_result = dedupe_pipeline_entries(
            track_slug="*",
            stage="all",
            include_done=False,
        )
        huey_logger.info(
            "[cleanup_inactive_pipeline_entries_daily] dedupe done removed=%s groups=%s",
            dedupe_result.get("entries_removed"),
            dedupe_result.get("duplicate_groups"),
        )
        logger.info(
            "[cleanup_inactive_pipeline_entries_daily] dedupe removed=%s groups=%s",
            dedupe_result.get("entries_removed"),
            dedupe_result.get("duplicate_groups"),
        )
    except Exception as e:
        status = "partial_failure"
        errors.append(f"dedupe_cleanup: {e}")
        logger.exception("[cleanup_inactive_pipeline_entries_daily] dedupe cleanup failed: %s", e)

    finished_at = timezone.now()
    payload = {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "checked": int(purge_result.get("checked") or 0),
        "removed_inactive": int(purge_result.get("removed_inactive") or 0),
        "active": int(purge_result.get("active") or 0),
        "unknown": int(purge_result.get("unknown") or 0),
        "dedupe_removed": int(dedupe_result.get("entries_removed") or 0),
        "dedupe_groups": int(dedupe_result.get("duplicate_groups") or 0),
        "errors": errors[:5],
    }
    cache.set(CLEANUP_STATUS_CACHE_KEY, payload, CLEANUP_STATUS_CACHE_TTL_SECONDS)
    return None


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
    """
    from .job_dedupe import dedupe_pipeline_entries

    try:
        return dedupe_pipeline_entries(
            track_slug=track,
            stage=stage,
            include_done=include_done,
        )
    except ValueError as e:
        logger.warning("[dedupe_pipeline_jobs_task] invalid params: %s", e)
        return {"status": "error", "message": str(e)}
