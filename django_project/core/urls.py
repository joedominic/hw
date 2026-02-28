from django.contrib import admin
from django.urls import path
from django.shortcuts import redirect
from ninja import NinjaAPI
from resume_app.api import router as resume_router

api = NinjaAPI()
api.add_router("/resume", resume_router)

urlpatterns = [
    path("", lambda request: redirect("api/docs")),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
