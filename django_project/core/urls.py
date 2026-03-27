from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI

from resume_app.api import router as resume_router
from resume_app import views as resume_views

api = NinjaAPI()
api.add_router("/resume", resume_router)

urlpatterns = [
    # Primary entry point: Django UI
    path("", resume_views.optimizer_view, name="home"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("resume/optimizer/", resume_views.optimizer_view, name="resume_optimizer"),
    path("resume/status/<int:resume_id>/", resume_views.optimizer_status_view, name="resume_status"),
    path("settings/", resume_views.settings_view, name="settings"),
    path("resume/prompts/", resume_views.prompt_library_view, name="prompt_library"),
    path("resume/llm-test/", resume_views.llm_test_view, name="llm_test"),
    path("workspace/workflows/", resume_views.workflow_list_view, name="workflow_list"),
    path("workspace/workflows/new/", resume_views.workflow_create_view, name="workflow_create"),
    path("workspace/workflows/<int:workflow_id>/edit/", resume_views.workflow_edit_view, name="workflow_edit"),
    path("workspace/workflows/<int:workflow_id>/delete/", resume_views.workflow_delete_view, name="workflow_delete"),
    path("jobs/search/", resume_views.job_search_view, name="jobs_search"),
    path("jobs/pipeline/", resume_views.pipeline_view, name="pipeline"),
    path("jobs/vetting/", resume_views.vetting_view, name="vetting"),
    path("jobs/applying/", resume_views.applying_view, name="applying"),
    path("jobs/done/", resume_views.done_view, name="done"),
    path("jobs/tracks/", resume_views.track_list_view, name="track_list"),
    path("jobs/tracks/<slug:slug>/delete/", resume_views.track_delete_view, name="track_delete"),
    path("jobs/automation/", resume_views.job_tasks_view, name="job_automation"),
    path("jobs/huey/", resume_views.huey_dashboard_view, name="huey_dashboard"),
    path(
        "jobs/huey/periodic/<str:task_name>/revoke/",
        resume_views.huey_periodic_revoke_view,
        name="huey_periodic_revoke",
    ),
    path(
        "jobs/huey/periodic/<str:task_name>/restore/",
        resume_views.huey_periodic_restore_view,
        name="huey_periodic_restore",
    ),
    path(
        "jobs/huey/flush-queue/",
        resume_views.huey_flush_queue_view,
        name="huey_flush_queue",
    ),
    path(
        "jobs/huey/run-cleanup/",
        resume_views.huey_run_cleanup_now_view,
        name="huey_run_cleanup_now",
    ),
    path("jobs/tasks/new/", resume_views.job_task_create_view, name="job_task_create"),
    path("jobs/tasks/<int:task_id>/edit/", resume_views.job_task_edit_view, name="job_task_edit"),
    path("jobs/tasks/<int:task_id>/run/", resume_views.job_task_run_now_view, name="job_task_run_now"),
    path("jobs/tasks/<int:task_id>/toggle/", resume_views.job_task_toggle_active_view, name="job_task_toggle_active"),
    path("jobs/<int:job_listing_id>/focus-breakdown/", resume_views.focus_breakdown_view, name="focus_breakdown"),
    path(
        "jobs/<int:job_listing_id>/focus-breakdown/<int:liked_job_id>/",
        resume_views.focus_alignment_view,
        name="focus_alignment",
    ),
]
