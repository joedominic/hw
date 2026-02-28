from ninja import Router, File, Schema, Form
from ninja.files import UploadedFile
from .models import UserResume, JobDescription, OptimizedResume, AgentLog
from .tasks import optimize_resume_task
from typing import List

router = Router()

class OptimizeRequest(Schema):
    job_description: str
    llm_provider: str
    api_key: str

class StatusResponse(Schema):
    status: str
    ats_score: int = None
    recruiter_score: int = None
    optimized_content: str = None
    logs: List[dict] = []

@router.post("/optimize")
def optimize_resume(request, payload: OptimizeRequest = Form(...), file: UploadedFile = File(...)):
    # 1. Save Resume
    user_resume = UserResume.objects.create(file=file)

    # 2. Save Job Description
    job_desc = JobDescription.objects.create(content=payload.job_description)

    # 3. Create OptimizedResume record
    optimized = OptimizedResume.objects.create(
        original_resume=user_resume,
        job_description=job_desc,
        status="queued"
    )

    # 4. Trigger Celery Task
    task = optimize_resume_task.delay(
        optimized.id,
        job_desc.id,
        payload.llm_provider,
        payload.api_key
    )

    return {"task_id": task.id, "resume_id": optimized.id}

@router.get("/status/{resume_id}", response=StatusResponse)
def get_status(request, resume_id: int):
    optimized = OptimizedResume.objects.get(id=resume_id)
    logs = AgentLog.objects.filter(optimized_resume=optimized).order_by('created_at')

    # Extract scores from logs if available
    ats_score = None
    recruiter_score = None
    for log in logs:
        if 'ats_score' in log.thought:
            ats_score = log.thought['ats_score']
        if 'recruiter_score' in log.thought:
            recruiter_score = log.thought['recruiter_score']

    return {
        "status": optimized.status,
        "ats_score": ats_score,
        "recruiter_score": recruiter_score,
        "optimized_content": optimized.optimized_content,
        "logs": [{"step": l.step_name, "thought": l.thought} for l in logs]
    }
