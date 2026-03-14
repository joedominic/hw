from huey.contrib.djhuey import db_task

from .models import OptimizedResume, AgentLog, LLMProviderConfig
from .agents import (
    create_workflow,
    get_llm,
    DEFAULT_WRITER_PROMPT,
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
)
try:
    from .agents import create_workflow_from_steps
except ImportError:
    create_workflow_from_steps = None  # older agents.py without configurable workflow
from .services import parse_pdf
from .callbacks import TokenUsageCallback
from .llm_services import is_auth_error
import time


@db_task()
def run_optimize_resume_task(
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
    """Huey async task: enqueue and run optimize_resume_task in a worker."""
    return optimize_resume_task(
        resume_id=resume_id,
        job_description_id=job_description_id,
        provider=provider,
        api_key=api_key,
        model=model,
        prompts=prompts,
        debug=debug,
        rate_limit_delay=rate_limit_delay,
        max_iterations=max_iterations,
        score_threshold=score_threshold,
        workflow_steps=workflow_steps,
        loop_to=loop_to,
    )


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
    Runs in a plain thread (not Celery).
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

        # Initialize LLM (may raise if key invalid)
        llm = get_llm(provider, api_key or None, model)

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

        # Initial State
        prompts = prompts or {}
        initial_state = {
            "resume_text": resume_text,
            "job_description": job_desc,
            "optimized_resume": "",
            "ats_score": 0,
            "recruiter_score": 0,
            "feedback": [],
            "iteration_count": 0,
            "llm": llm,
            "writer_prompt_template": prompts.get("writer") or DEFAULT_WRITER_PROMPT,
            "ats_judge_prompt_template": prompts.get("ats_judge") or DEFAULT_ATS_JUDGE_PROMPT,
            "recruiter_judge_prompt_template": prompts.get("recruiter_judge") or DEFAULT_RECRUITER_JUDGE_PROMPT,
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
                    step_display = prev_node
                    if steps and prev_node.startswith("step_"):
                        try:
                            idx = int(prev_node.split("_", 1)[1])
                            if 0 <= idx < len(steps):
                                step_display = steps[idx]
                        except (ValueError, IndexError):
                            pass
                    AgentLog.objects.create(
                        optimized_resume=optimized_resume,
                        step_name=step_display,
                        thought=accumulated[prev_node],
                    )
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
            step_display = prev_node
            if steps and prev_node.startswith("step_"):
                try:
                    idx = int(prev_node.split("_", 1)[1])
                    if 0 <= idx < len(steps):
                        step_display = steps[idx]
                except (ValueError, IndexError):
                    pass
            AgentLog.objects.create(
                optimized_resume=optimized_resume,
                step_name=step_display,
                thought=accumulated[prev_node],
            )

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
