from django.contrib import admin
from .models import LLMProviderConfig, JobListing, JobMatchResult, JobListingEmbedding, UserDisqualifier, OptimizerWorkflow


@admin.register(LLMProviderConfig)
class LLMProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("provider", "default_model", "last_validated_at", "updated_at")
    readonly_fields = ("last_validated_at", "created_at", "updated_at")


@admin.register(JobListing)
class JobListingAdmin(admin.ModelAdmin):
    list_display = ("title", "company_name", "source", "fetched_at")
    list_filter = ("source",)
    search_fields = ("title", "company_name")


@admin.register(JobMatchResult)
class JobMatchResultAdmin(admin.ModelAdmin):
    list_display = ("job_listing", "resume", "fit_score", "status", "analyzed_at")
    list_filter = ("status",)


@admin.register(JobListingEmbedding)
class JobListingEmbeddingAdmin(admin.ModelAdmin):
    list_display = ("job_listing", "created_at")


@admin.register(UserDisqualifier)
class UserDisqualifierAdmin(admin.ModelAdmin):
    list_display = ("phrase", "created_at")
    search_fields = ("phrase",)


@admin.register(OptimizerWorkflow)
class OptimizerWorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "max_iterations", "score_threshold", "updated_at")
    list_filter = ("max_iterations",)
