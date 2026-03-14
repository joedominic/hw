from ninja import Router, File, Schema, Form
from ninja.files import UploadedFile
from ninja.errors import HttpError
from django.shortcuts import get_object_or_404
from django.http import FileResponse
from django.utils import timezone
from .models import UserResume, JobDescription, OptimizedResume, AgentLog, LLMProviderConfig, OptimizerWorkflow
from .jobs_api import router as jobs_router
from .tasks import run_optimize_resume_task
from .agents import (
    DEFAULT_WRITER_PROMPT,
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_FIT_CHECK_PROMPT,
    DEFAULT_MATCHING_PROMPT,
    VALID_STEP_IDS,
    get_llm,
    run_fit_check,
    writer_node,
    ats_judge_node,
    recruiter_judge_node,
)
from .services import parse_pdf
from .crypto import encrypt_api_key, decrypt_api_key
from .llm_services import list_models_for_provider, is_auth_error, DEFAULT_MODELS, LLM_PROVIDERS
from typing import List, Optional
from django.conf import settings
import json
import io
import tempfile
import os
import logging
import traceback

logger = logging.getLogger(__name__)

router = Router()
router.add_router("/jobs", jobs_router)

MAX_JOB_DESCRIPTION_LENGTH = 50_000
MAX_RESUME_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = ("application/pdf",)
PDF_EXTENSION = ".pdf"


def _require_api_auth(request):
    """
    Simple optional auth: if settings.API_ACCESS_TOKEN is set, require
    header X-Api-Token to match. If not set, no auth is enforced.
    """
    token = getattr(settings, "API_ACCESS_TOKEN", None)
    if not token:
        return
    provided = request.headers.get("X-Api-Token") or request.META.get("HTTP_X_API_TOKEN")
    if not provided or provided != token:
        raise HttpError(401, "Missing or invalid API access token.")

class OptimizeRequest(Schema):
    job_description: str
    llm_provider: str
    llm_model: Optional[str] = None  # from provider's list; default from config or built-in
    api_key: Optional[str] = None  # optional when key stored in DB or env
    prompt_writer: Optional[str] = None  # override default writer prompt template
    prompt_ats_judge: Optional[str] = None  # override default ATS judge prompt template
    prompt_recruiter_judge: Optional[str] = None  # override default recruiter judge prompt template
    debug: Optional[bool] = None  # when True, log full prompts sent to LLM in agent logs
    workflow_steps: Optional[str] = None  # JSON array of step ids, e.g. ["writer","ats_judge","recruiter_judge"]
    loop_to: Optional[str] = None  # step to loop back to after last step; empty = single pass
    score_threshold: Optional[int] = None  # exit when avg score >= this (0-100, default 85)

class StatusResponse(Schema):
    status: str
    status_display: Optional[str] = None
    ats_score: Optional[int] = None
    recruiter_score: Optional[int] = None
    optimized_content: Optional[str] = None
    error_message: Optional[str] = None
    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None
    logs: List[dict] = []

class PromptsResponse(Schema):
    writer: str
    ats_judge: str
    recruiter_judge: str
    matching: str
    insights: str

class FitCheckRequest(Schema):
    job_description: str
    llm_provider: str
    llm_model: Optional[str] = None
    api_key: Optional[str] = None
    prompt_fit_check: Optional[str] = None

class FitCheckResponse(Schema):
    score: int
    reasoning: str
    thoughts: str = ""


class RunStepRequest(Schema):
    step: str  # writer | ats_judge | recruiter_judge
    job_description: str
    use_resume_id: Optional[int] = None
    optimized_resume: Optional[str] = None  # for ats_judge, recruiter_judge
    feedback: Optional[str] = None  # for writer (comma-separated)
    prompt_writer: Optional[str] = None
    prompt_ats_judge: Optional[str] = None
    prompt_recruiter_judge: Optional[str] = None
    llm_provider: str
    llm_model: Optional[str] = None
    debug: Optional[bool] = None


class RunStepResponse(Schema):
    """Response for POST /run-step. Frontend: use output.debug_prompt for Input, output for Output, output.input_tokens/output_tokens for token count. error is set when the step failed."""
    step: str  # "writer" | "ats_judge" | "recruiter_judge"
    output: dict  # writer: {optimized_resume, debug_prompt?, input_tokens?, output_tokens?}; ats_judge: {ats_score, feedback, debug_prompt?, input_tokens?, output_tokens?, response_json?}; recruiter_judge: {recruiter_score, feedback, debug_prompt?, input_tokens?, output_tokens?, response_json?}
    error: Optional[str] = None  # when set, step failed; frontend should show step name + error


@router.post("/run-step", response=RunStepResponse)
def run_step(
    request,
    payload: RunStepRequest = Form(...),
    file: Optional[UploadedFile] = File(None),
):
    """Run a single optimizer step (writer, ats_judge, or recruiter_judge) and return its output."""
    step = (payload.step or "").strip().lower()
    logger.warning("[run_step] step=%s, job_desc_len=%s, optimized_resume_len=%s", step, len(payload.job_description or ""), len(payload.optimized_resume or ""))
    if step not in ("writer", "ats_judge", "recruiter_judge"):
        raise HttpError(400, "step must be one of: writer, ats_judge, recruiter_judge")
    if not (payload.job_description or "").strip():
        raise HttpError(400, "job_description is required")
    if payload.llm_provider not in LLM_PROVIDERS:
        raise HttpError(400, f"llm_provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")

    # Resolve API key and model
    api_key = None
    config = LLMProviderConfig.objects.filter(provider=payload.llm_provider).first()
    if config and config.encrypted_api_key:
        api_key = decrypt_api_key(config.encrypted_api_key)
    if not api_key:
        raise HttpError(400, f"API key required for {payload.llm_provider}. Connect an API key first.")
    model = payload.llm_model or (config.default_model if config else None) or None
    llm = get_llm(payload.llm_provider, api_key, model)

    prompts = {
        "writer": payload.prompt_writer or DEFAULT_WRITER_PROMPT,
        "ats_judge": payload.prompt_ats_judge or DEFAULT_ATS_JUDGE_PROMPT,
        "recruiter_judge": payload.prompt_recruiter_judge or DEFAULT_RECRUITER_JUDGE_PROMPT,
    }
    # Step-by-step always runs in debug mode
    debug = True
    # Persist edited prompts to session so they're used on next page load
    if hasattr(request, "session"):
        request.session["optimizer_prompts"] = {
            "writer": payload.prompt_writer or "",
            "ats_judge": payload.prompt_ats_judge or "",
            "recruiter_judge": payload.prompt_recruiter_judge or "",
        }
        request.session.modified = True

    def _get_resume_text():
        if file and getattr(file, "size", 0) and getattr(file, "name", "").strip().lower().endswith(PDF_EXTENSION):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file.read())
                tmp_path = tmp.name
            try:
                return parse_pdf(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if payload.use_resume_id:
            ur = get_object_or_404(UserResume, id=payload.use_resume_id)
            return parse_pdf(ur.file.path)
        return None

    try:
        if step == "writer":
            resume_text = _get_resume_text()
            if not resume_text:
                raise HttpError(400, "For writer step provide a resume file upload or use_resume_id.")
            feedback = [f.strip() for f in (payload.feedback or "").split(",") if f.strip()]
            state = {
                "resume_text": resume_text,
                "job_description": payload.job_description,
                "optimized_resume": "",
                "ats_score": 0,
                "recruiter_score": 0,
                "feedback": feedback,
                "iteration_count": 0,
                "llm": llm,
                "writer_prompt_template": prompts["writer"],
                "ats_judge_prompt_template": prompts["ats_judge"],
                "recruiter_judge_prompt_template": prompts["recruiter_judge"],
                "debug": debug,
                "max_iterations": 3,
            }
            out = writer_node(state)
            return RunStepResponse(step="writer", output={"optimized_resume": out.get("optimized_resume", ""), "debug_prompt": out.get("debug_prompt"), "input_tokens": out.get("input_tokens"), "output_tokens": out.get("output_tokens"), "tokens_estimated": out.get("tokens_estimated")})

        if step in ("ats_judge", "recruiter_judge"):
            optimized_resume = (payload.optimized_resume or "").strip()
            if not optimized_resume:
                raise HttpError(400, "For ATS/Recruiter steps provide optimized_resume (current draft text).")
            state = {
                "resume_text": "",
                "job_description": payload.job_description,
                "optimized_resume": optimized_resume,
                "ats_score": 0,
                "recruiter_score": 0,
                "feedback": [],
                "iteration_count": 0,
                "llm": llm,
                "writer_prompt_template": prompts["writer"],
                "ats_judge_prompt_template": prompts["ats_judge"],
                "recruiter_judge_prompt_template": prompts["recruiter_judge"],
                "debug": debug,
                "max_iterations": 3,
            }
            if step == "ats_judge":
                out = ats_judge_node(state)
                fb = out.get("feedback")
                if isinstance(fb, list) and fb:
                    fb = fb[-1]
                elif not isinstance(fb, str):
                    fb = str(fb) if fb else ""
                output = {"ats_score": out.get("ats_score"), "feedback": fb, "debug_prompt": out.get("debug_prompt"), "input_tokens": out.get("input_tokens"), "output_tokens": out.get("output_tokens"), "tokens_estimated": out.get("tokens_estimated")}
                if out.get("last_ats_json") is not None:
                    output["response_json"] = out["last_ats_json"]
                return RunStepResponse(step="ats_judge", output=output)
            else:
                out = recruiter_judge_node(state)
                fb = out.get("feedback")
                if isinstance(fb, list) and fb:
                    fb = fb[-1]
                elif not isinstance(fb, str):
                    fb = str(fb) if fb else ""
                output = {"recruiter_score": out.get("recruiter_score"), "feedback": fb, "debug_prompt": out.get("debug_prompt"), "input_tokens": out.get("input_tokens"), "output_tokens": out.get("output_tokens"), "tokens_estimated": out.get("tokens_estimated")}
                if out.get("last_recruiter_json") is not None:
                    output["response_json"] = out["last_recruiter_json"]
                return RunStepResponse(step="recruiter_judge", output=output)
    except HttpError:
        raise
    except Exception as e:
        err_msg = str(e).strip().replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if len(err_msg) > 400:
            err_msg = err_msg[:400] + "..."
        logger.exception("[run_step] step=%s failed: %s", step, e)
        logger.debug("[run_step] traceback: %s", traceback.format_exc())
        return RunStepResponse(step=step, output={}, error=err_msg or "Step failed.")


@router.post("/fit-check", response=FitCheckResponse)
def fit_check(request, payload: FitCheckRequest = Form(...), file: UploadedFile = File(...)):
    """Assess candidate-job fit (0-100). If score < 50, UI should ask user whether to proceed with optimization."""
    _require_api_auth(request)
    if payload.llm_provider not in LLM_PROVIDERS:
        raise HttpError(400, f"llm_provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")
    if len(payload.job_description) > MAX_JOB_DESCRIPTION_LENGTH:
        raise HttpError(400, f"job_description must be at most {MAX_JOB_DESCRIPTION_LENGTH} characters")
    if getattr(file, "size", 0) > MAX_RESUME_FILE_SIZE_BYTES:
        raise HttpError(400, f"Resume file must be at most {MAX_RESUME_FILE_SIZE_BYTES // (1024*1024)} MB")
    if not (getattr(file, "name", "") or "").strip().lower().endswith(PDF_EXTENSION):
        raise HttpError(400, "Resume file must be a PDF")

    api_key = payload.api_key
    if not api_key:
        config = LLMProviderConfig.objects.filter(provider=payload.llm_provider).first()
        if config and config.encrypted_api_key:
            api_key = decrypt_api_key(config.encrypted_api_key)
        if not api_key:
            raise HttpError(400, f"API key required for {payload.llm_provider}.")

    model = payload.llm_model
    if not model:
        config = LLMProviderConfig.objects.filter(provider=payload.llm_provider).first()
        model = config.default_model if config else None
    model = model or None

    llm = get_llm(payload.llm_provider, api_key or None, model)
    prompt_template = payload.prompt_fit_check or None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(file.read())
            tmp_path = tmp.name
        try:
            resume_text = parse_pdf(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        raise HttpError(400, f"Could not read PDF: {e}") from e

    result = run_fit_check(resume_text, payload.job_description, llm, prompt_template)
    return result

@router.get("/prompts", response=PromptsResponse)
def get_prompts(request):
    """Return default LLM prompt templates. Use placeholders: {resume_text}, {job_description}, {feedback}, {optimized_resume}, {job_descriptions}."""
    from .prompts import DEFAULT_INSIGHTS_PROMPT
    return {
        "writer": DEFAULT_WRITER_PROMPT,
        "ats_judge": DEFAULT_ATS_JUDGE_PROMPT,
        "recruiter_judge": DEFAULT_RECRUITER_JUDGE_PROMPT,
        "matching": DEFAULT_MATCHING_PROMPT,
        "insights": DEFAULT_INSIGHTS_PROMPT,
    }

@router.post("/optimize")
def optimize_resume(request, payload: OptimizeRequest = Form(...), file: UploadedFile = File(...)):
    _require_api_auth(request)
    # Validation
    if payload.llm_provider not in LLM_PROVIDERS:
        raise HttpError(400, f"llm_provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")
    if len(payload.job_description) > MAX_JOB_DESCRIPTION_LENGTH:
        raise HttpError(400, f"job_description must be at most {MAX_JOB_DESCRIPTION_LENGTH} characters")
    if getattr(file, "size", 0) > MAX_RESUME_FILE_SIZE_BYTES:
        raise HttpError(400, f"Resume file must be at most {MAX_RESUME_FILE_SIZE_BYTES // (1024*1024)} MB")
    if not (getattr(file, "name", "") or "").strip().lower().endswith(PDF_EXTENSION):
        raise HttpError(400, "Resume file must be a PDF")
    content_type = getattr(file, "content_type", "") or ""
    if content_type and content_type.lower() not in ALLOWED_CONTENT_TYPES:
        raise HttpError(400, "Resume file must have content type application/pdf")

    # 1. Save Resume (retain original filename for display)
    original_name = (getattr(file, "name", "") or "resume.pdf").strip()
    if original_name and "/" in original_name:
        original_name = original_name.split("/")[-1]
    original_name = (original_name or "resume.pdf")[:255]
    user_resume = UserResume.objects.create(file=file, original_filename=original_name)

    # 2. Save Job Description
    job_desc = JobDescription.objects.create(content=payload.job_description)

    # 3. Create OptimizedResume record
    optimized = OptimizedResume.objects.create(
        original_resume=user_resume,
        job_description=job_desc,
        status=OptimizedResume.STATUS_QUEUED
    )

    # Resolve API key: request > stored config > env
    api_key = payload.api_key
    if not api_key:
        config = LLMProviderConfig.objects.filter(provider=payload.llm_provider).first()
        if config and config.encrypted_api_key:
            api_key = decrypt_api_key(config.encrypted_api_key)
        if not api_key:
            raise HttpError(400, f"API key required for {payload.llm_provider}. Enter key in LLM Config and connect, or pass api_key.")

    model = payload.llm_model
    if not model:
        config = LLMProviderConfig.objects.filter(provider=payload.llm_provider).first()
        model = config.default_model if config else None
    model = model or None

    prompts = None
    if payload.prompt_writer or payload.prompt_ats_judge or payload.prompt_recruiter_judge:
        prompts = {
            "writer": payload.prompt_writer or None,
            "ats_judge": payload.prompt_ats_judge or None,
            "recruiter_judge": payload.prompt_recruiter_judge or None,
        }

    # Debug flag: form sends "true"/"false" as string; read from POST so it's not lost
    debug_val = request.POST.get("debug", "") if hasattr(request, "POST") else ""
    debug = str(debug_val).lower() in ("true", "1", "on")

    rate_limit_delay = 0
    try:
        rld = request.POST.get("rate_limit_delay", "") if hasattr(request, "POST") else ""
        if rld not in ("", None):
            rate_limit_delay = float(rld)
    except (TypeError, ValueError):
        pass

    max_iterations = 3
    try:
        mi = request.POST.get("max_iterations", "") if hasattr(request, "POST") else ""
        if mi not in ("", None):
            max_iterations = int(mi)
    except (TypeError, ValueError):
        pass

    # Parse optional workflow step order (JSON array string)
    workflow_steps = None
    if payload.workflow_steps and (payload.workflow_steps or "").strip():
        try:
            raw = json.loads(payload.workflow_steps)
            if isinstance(raw, list) and raw:
                invalid = [s for s in raw if not isinstance(s, str) or s not in VALID_STEP_IDS]
                if invalid:
                    raise HttpError(400, f"Invalid workflow_steps: {invalid}. Allowed: {sorted(VALID_STEP_IDS)}")
                workflow_steps = raw
        except json.JSONDecodeError as e:
            raise HttpError(400, f"workflow_steps must be a JSON array of step ids: {e}") from e
    loop_to = (payload.loop_to or "").strip() or None
    if loop_to is not None and loop_to not in VALID_STEP_IDS:
        raise HttpError(400, f"loop_to must be one of: {sorted(VALID_STEP_IDS)}")

    score_threshold = 85
    if payload.score_threshold is not None:
        st = int(payload.score_threshold)
        if not 0 <= st <= 100:
            raise HttpError(400, "score_threshold must be between 0 and 100")
        score_threshold = st

    # Enqueue Huey task (Redis-backed worker)
    result = run_optimize_resume_task(
        optimized.id,
        job_desc.id,
        payload.llm_provider,
        api_key or "",
        model,
        prompts=prompts,
        debug=debug,
        rate_limit_delay=rate_limit_delay,
        max_iterations=max_iterations,
        score_threshold=score_threshold,
        workflow_steps=workflow_steps,
        loop_to=loop_to,
    )
    task_id = result.id if result else None
    return {"task_id": task_id, "resume_id": optimized.id}

@router.get("/status/{resume_id}", response=StatusResponse)
def get_status(request, resume_id: int):
    _require_api_auth(request)
    optimized = get_object_or_404(OptimizedResume, id=resume_id)
    logs = AgentLog.objects.filter(optimized_resume=optimized).order_by('created_at')

    # Use stored scores; fall back to last log for backward compat
    ats_score = optimized.ats_score
    recruiter_score = optimized.recruiter_score
    if ats_score is None or recruiter_score is None:
        for log in logs:
            if 'ats_score' in log.thought:
                ats_score = log.thought['ats_score']
            if 'recruiter_score' in log.thought:
                recruiter_score = log.thought['recruiter_score']

    # Human-readable status: prefer status_display when running
    status_text = optimized.status_display or optimized.status

    return {
        "status": status_text,
        "status_display": optimized.status_display or None,
        "ats_score": ats_score,
        "recruiter_score": recruiter_score,
        "optimized_content": optimized.optimized_content,
        "error_message": optimized.error_message,
        "total_input_tokens": optimized.total_input_tokens,
        "total_output_tokens": optimized.total_output_tokens,
        "logs": [{"step": l.step_name, "thought": l.thought} for l in logs]
    }


CANCELLED_MESSAGE = "Cancelled by user"


@router.post("/status/{resume_id}/cancel")
def cancel_optimization(request, resume_id: int):
    """Mark a running or queued optimization as cancelled. The background task will stop on its next check."""
    _require_api_auth(request)
    optimized = get_object_or_404(OptimizedResume, id=resume_id)
    if optimized.status not in (OptimizedResume.STATUS_QUEUED, OptimizedResume.STATUS_RUNNING):
        return {"success": False, "message": "Optimization is not running or queued."}
    optimized.status = OptimizedResume.STATUS_FAILED
    optimized.error_message = CANCELLED_MESSAGE
    optimized.status_display = ""
    optimized.save(update_fields=["status", "error_message", "status_display"])
    return {"success": True, "message": "Optimization cancelled."}


# --- Workflows (saved custom optimizer workflows) ---

class WorkflowSchema(Schema):
    id: int
    name: str
    steps: List[str]
    loop_to: str
    max_iterations: int
    score_threshold: int


class WorkflowCreateUpdate(Schema):
    name: str
    steps: List[str]
    loop_to: Optional[str] = ""
    max_iterations: Optional[int] = 3
    score_threshold: Optional[int] = 85


def _validate_workflow_steps(steps: list) -> None:
    if not steps:
        raise HttpError(400, "steps must not be empty")
    invalid = [s for s in steps if not isinstance(s, str) or s not in VALID_STEP_IDS]
    if invalid:
        raise HttpError(400, f"Invalid step id(s): {invalid}. Allowed: {sorted(VALID_STEP_IDS)}")


@router.get("/workflows", response=List[WorkflowSchema])
def list_workflows(request):
    """List all saved optimizer workflows."""
    return [
        {
            "id": w.id,
            "name": w.name,
            "steps": w.steps,
            "loop_to": w.loop_to or "",
            "max_iterations": w.max_iterations,
            "score_threshold": w.score_threshold,
        }
        for w in OptimizerWorkflow.objects.all()
    ]


@router.get("/workflows/{workflow_id}", response=WorkflowSchema)
def get_workflow(request, workflow_id: int):
    w = get_object_or_404(OptimizerWorkflow, id=workflow_id)
    return {
        "id": w.id,
        "name": w.name,
        "steps": w.steps,
        "loop_to": w.loop_to or "",
        "max_iterations": w.max_iterations,
        "score_threshold": w.score_threshold,
    }


@router.post("/workflows", response=WorkflowSchema)
def create_workflow_api(request, payload: WorkflowCreateUpdate):
    _validate_workflow_steps(payload.steps)
    loop_to = (payload.loop_to or "").strip() or ""
    if loop_to and loop_to not in VALID_STEP_IDS:
        raise HttpError(400, f"loop_to must be one of: {sorted(VALID_STEP_IDS)} or empty")
    max_it = payload.max_iterations if payload.max_iterations is not None else 3
    max_it = max(1, min(5, max_it))
    score_t = payload.score_threshold if payload.score_threshold is not None else 85
    score_t = max(0, min(100, score_t))
    w = OptimizerWorkflow.objects.create(
        name=payload.name.strip(),
        steps=payload.steps,
        loop_to=loop_to,
        max_iterations=max_it,
        score_threshold=score_t,
    )
    return {
        "id": w.id,
        "name": w.name,
        "steps": w.steps,
        "loop_to": w.loop_to or "",
        "max_iterations": w.max_iterations,
        "score_threshold": w.score_threshold,
    }


@router.put("/workflows/{workflow_id}", response=WorkflowSchema)
def update_workflow(request, workflow_id: int, payload: WorkflowCreateUpdate):
    w = get_object_or_404(OptimizerWorkflow, id=workflow_id)
    _validate_workflow_steps(payload.steps)
    loop_to = (payload.loop_to or "").strip() or ""
    if loop_to and loop_to not in VALID_STEP_IDS:
        raise HttpError(400, f"loop_to must be one of: {sorted(VALID_STEP_IDS)} or empty")
    max_it = payload.max_iterations if payload.max_iterations is not None else 3
    max_it = max(1, min(5, max_it))
    score_t = payload.score_threshold if payload.score_threshold is not None else 85
    score_t = max(0, min(100, score_t))
    w.name = payload.name.strip()
    w.steps = payload.steps
    w.loop_to = loop_to
    w.max_iterations = max_it
    w.score_threshold = score_t
    w.save()
    return {
        "id": w.id,
        "name": w.name,
        "steps": w.steps,
        "loop_to": w.loop_to or "",
        "max_iterations": w.max_iterations,
        "score_threshold": w.score_threshold,
    }


@router.delete("/workflows/{workflow_id}")
def delete_workflow(request, workflow_id: int):
    w = get_object_or_404(OptimizerWorkflow, id=workflow_id)
    w.delete()
    return {"success": True}


# --- LLM config: connect (validate + save key), list models ---

class ConnectRequest(Schema):
    provider: str
    api_key: str


@router.post("/llm/connect")
def llm_connect(request, payload: ConnectRequest):
    """Validate API key by listing models; on success save key (encrypted) and return models."""
    if payload.provider not in LLM_PROVIDERS:
        raise HttpError(400, f"provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")
    if not payload.api_key or not payload.api_key.strip():
        raise HttpError(400, "api_key is required")
    try:
        models = list_models_for_provider(payload.provider, payload.api_key.strip())
    except Exception as e:
        err = str(e)
        if is_auth_error(e):
            LLMProviderConfig.objects.filter(provider=payload.provider).delete()
            raise HttpError(401, f"Invalid API key: {err}")
        raise HttpError(400, err)
    config, _ = LLMProviderConfig.objects.get_or_create(provider=payload.provider, defaults={"encrypted_api_key": ""})
    config.encrypted_api_key = encrypt_api_key(payload.api_key.strip())
    config.last_validated_at = timezone.now()
    config.save(update_fields=["encrypted_api_key", "last_validated_at", "updated_at"])
    return {"success": True, "models": models}


@router.get("/llm/models")
def llm_models(request, provider: str):
    """Return list of models for provider using stored API key. 401 if no key or key invalid."""
    if provider not in LLM_PROVIDERS:
        raise HttpError(400, f"provider must be one of: {', '.join(sorted(LLM_PROVIDERS))}")
    config = LLMProviderConfig.objects.filter(provider=provider).first()
    if not config or not config.encrypted_api_key:
        raise HttpError(401, "No API key stored. Enter key and click Connect.")
    api_key = decrypt_api_key(config.encrypted_api_key)
    if not api_key:
        config.encrypted_api_key = ""
        config.last_validated_at = None
        config.save(update_fields=["encrypted_api_key", "last_validated_at", "updated_at"])
        raise HttpError(401, "Stored key invalid. Please enter a new key and Connect.")
    try:
        models = list_models_for_provider(provider, api_key)
        config.last_validated_at = timezone.now()
        config.save(update_fields=["last_validated_at", "updated_at"])
        return {"models": models, "default_model": config.default_model or DEFAULT_MODELS.get(provider)}
    except Exception as e:
        if is_auth_error(e):
            config.encrypted_api_key = ""
            config.last_validated_at = None
            config.save(update_fields=["encrypted_api_key", "last_validated_at", "updated_at"])
            raise HttpError(401, f"API key no longer valid: {e}")
        raise HttpError(400, str(e))


@router.post("/llm/set-default-model")
def llm_set_default_model(request, provider: str, model: str):
    """Set default model for provider (stored config)."""
    if provider not in LLM_PROVIDERS:
        raise HttpError(400, "Invalid provider")
    config = LLMProviderConfig.objects.filter(provider=provider).first()
    if not config:
        raise HttpError(404, "Connect with an API key first")
    config.default_model = model
    config.save(update_fields=["default_model", "updated_at"])
    return {"success": True}


def _parse_markdown_blocks(content: str):
    """Yield (block_type, text). block_type: heading1, heading2, bullet, paragraph."""
    if not content:
        return
    for line in (content or "").replace("\r", "").split("\n"):
        raw = line
        line = line.strip()
        if not line:
            continue
        if line.startswith("# "):
            yield ("heading1", line[2:].strip())
        elif line.startswith("## "):
            yield ("heading2", line[3:].strip())
        elif line.startswith("- ") or line.startswith("* "):
            yield ("bullet", line[2:].strip())
        else:
            yield ("paragraph", raw.strip())


def _split_bold_spans(text: str):
    """Yield (is_bold, segment) for text with **bold** markers."""
    import re
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    for p in parts:
        if not p:
            continue
        if p.startswith("**") and p.endswith("**"):
            yield (True, p[2:-2])
        else:
            yield (False, p)


def _build_export_pdf(content: str) -> io.BytesIO:
    """Build PDF from markdown-style content with headings, bullets, bold."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import inch
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        width, height = letter
        y = height - inch
        line_height = 14
        for block_type, text in _parse_markdown_blocks(content):
            if y < inch + line_height * 2:
                c.showPage()
                y = height - inch
            if block_type == "heading1":
                c.setFont("Helvetica-Bold", 16)
                c.drawString(inch, y, text[:120])
                y -= line_height + 4
            elif block_type == "heading2":
                c.setFont("Helvetica-Bold", 12)
                c.drawString(inch, y, text[:120])
                y -= line_height + 2
            elif block_type == "bullet":
                c.setFont("Helvetica", 10)
                c.drawString(inch, y, chr(8226) + " " + text[:95])
                y -= line_height
            else:
                c.setFont("Helvetica", 10)
                x = inch
                for is_bold, segment in _split_bold_spans(text):
                    if is_bold:
                        c.setFont("Helvetica-Bold", 10)
                    c.drawString(x, y, segment[:100])
                    x += c.stringWidth(segment[:100], "Helvetica-Bold" if is_bold else "Helvetica", 10)
                    if is_bold:
                        c.setFont("Helvetica", 10)
                y -= line_height
        c.save()
        buf.seek(0)
        return buf
    except ImportError:
        return None


def _build_export_docx(content: str) -> io.BytesIO:
    """Build Word from markdown-style content with headings, bullets, bold."""
    try:
        from docx import Document
        from docx.shared import Pt
        doc = Document()
        for block_type, text in _parse_markdown_blocks(content):
            if block_type == "heading1":
                p = doc.add_heading(text, level=0)
                p.paragraph_format.space_after = Pt(8)
            elif block_type == "heading2":
                p = doc.add_heading(text, level=1)
                p.paragraph_format.space_after = Pt(6)
            elif block_type == "bullet":
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Pt(18)
                p.add_run(chr(8226) + " ")
                for is_bold, segment in _split_bold_spans(text):
                    r = p.add_run(segment)
                    r.bold = is_bold
                p.paragraph_format.space_after = Pt(4)
            else:
                p = doc.add_paragraph()
                for is_bold, segment in _split_bold_spans(text):
                    r = p.add_run(segment)
                    r.bold = is_bold
                p.paragraph_format.space_after = Pt(6)
        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf
    except ImportError:
        return None


@router.get("/export/{resume_id}/pdf")
def export_pdf(request, resume_id: int):
    """Export optimized resume as PDF. Returns 404 if not found or not completed."""
    optimized = get_object_or_404(OptimizedResume, id=resume_id)
    if optimized.status != OptimizedResume.STATUS_COMPLETED or not optimized.optimized_content:
        raise HttpError(404, "Optimized resume not ready for export")
    buf = _build_export_pdf(optimized.optimized_content)
    if buf is None:
        raise HttpError(503, "PDF export requires reportlab; install with: pip install reportlab")
    return FileResponse(buf, as_attachment=True, filename="optimized_resume.pdf", content_type="application/pdf")


@router.get("/export/{resume_id}/docx")
def export_docx(request, resume_id: int):
    """Export optimized resume as Word. Returns 404 if not found or not completed."""
    optimized = get_object_or_404(OptimizedResume, id=resume_id)
    if optimized.status != OptimizedResume.STATUS_COMPLETED or not optimized.optimized_content:
        raise HttpError(404, "Optimized resume not ready for export")
    buf = _build_export_docx(optimized.optimized_content)
    if buf is None:
        raise HttpError(503, "Word export requires python-docx; install with: pip install python-docx")
    return FileResponse(buf, as_attachment=True, filename="optimized_resume.docx", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
