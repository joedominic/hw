from celery import shared_task
from .models import OptimizedResume, AgentLog
from .agents import create_workflow, get_llm
from .services import parse_pdf
import os

@shared_task(bind=True)
def optimize_resume_task(self, resume_id, job_description_id, provider, api_key):
    try:
        optimized_resume = OptimizedResume.objects.get(id=resume_id)
        job_desc = optimized_resume.job_description.content

        # Parse PDF
        resume_text = parse_pdf(optimized_resume.original_resume.file.path)

        # Initialize LLM
        llm = get_llm(provider, api_key)

        # Setup Graph
        app = create_workflow()

        # Initial State
        initial_state = {
            "resume_text": resume_text,
            "job_description": job_desc,
            "optimized_resume": "",
            "ats_score": 0,
            "recruiter_score": 0,
            "feedback": [],
            "iteration_count": 0,
            "llm": llm
        }

        # Run Graph
        last_state = initial_state
        for output in app.stream(initial_state):
            # Save logs for each step
            for node_name, state_update in output.items():
                # Merge update into our tracking state
                last_state.update(state_update)

                AgentLog.objects.create(
                    optimized_resume=optimized_resume,
                    step_name=node_name,
                    thought=state_update
                )

                # Update status for UI polling
                if 'ats_score' in state_update or 'recruiter_score' in state_update:
                    optimized_resume.status = f"Scoring: ATS={last_state.get('ats_score', 'N/A')}, Recruiter={last_state.get('recruiter_score', 'N/A')}"
                elif 'optimized_resume' in state_update:
                    optimized_resume.status = f"Drafting iteration {state_update.get('iteration_count', '...')}"

                optimized_resume.save()

        # Final update using the accumulated state
        optimized_resume.optimized_content = last_state['optimized_resume']
        optimized_resume.status = "completed"
        optimized_resume.save()

        return {"status": "success", "resume_id": resume_id}

    except Exception as e:
        if 'optimized_resume' in locals():
            optimized_resume.status = f"failed: {str(e)}"
            optimized_resume.save()
        return {"status": "error", "message": str(e)}
