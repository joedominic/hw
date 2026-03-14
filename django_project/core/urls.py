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
    path("resume/workflows/", resume_views.workflow_list_view, name="workflow_list"),
    path("resume/workflows/new/", resume_views.workflow_create_view, name="workflow_create"),
    path("resume/workflows/<int:workflow_id>/edit/", resume_views.workflow_edit_view, name="workflow_edit"),
    path("resume/workflows/<int:workflow_id>/delete/", resume_views.workflow_delete_view, name="workflow_delete"),
    path("jobs/search/", resume_views.job_search_view, name="jobs_search"),
    path("jobs/<int:job_listing_id>/focus-breakdown/", resume_views.focus_breakdown_view, name="focus_breakdown"),
    path(
        "jobs/<int:job_listing_id>/focus-breakdown/<int:liked_job_id>/",
        resume_views.focus_alignment_view,
        name="focus_alignment",
    ),
    path("jobs/keywords/", resume_views.keyword_search_view, name="jobs_keywords"),
]
