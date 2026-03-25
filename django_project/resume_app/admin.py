from django.contrib import admin
from .models import (
    AppAutomationSettings,
    LLMProviderPreference,
    LLMProviderConfig,
    JobListing,
    JobMatchResult,
    JobListingEmbedding,
    UserDisqualifier,
    OptimizerWorkflow,
    UserPromptProfile,
)


@admin.register(AppAutomationSettings)
class AppAutomationSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pipeline_to_vetting_enabled",
        "pipeline_preference_margin_min",
        "vetting_to_applying_enabled",
        "vetting_interview_probability_min",
        "applying_optimizer_workflow",
        "updated_at",
    )


@admin.register(LLMProviderConfig)
class LLMProviderConfigAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "is_active",
        "priority",
        "default_model",
        "last_validated_at",
        "updated_at",
    )
    readonly_fields = ("last_validated_at", "created_at", "updated_at")


@admin.register(LLMProviderPreference)
class LLMProviderPreferenceAdmin(admin.ModelAdmin):
    list_display = ("provider_config", "model", "priority", "updated_at")
    list_filter = ("provider_config__provider",)


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


@admin.register(UserPromptProfile)
class UserPromptProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "updated_at")
    readonly_fields = ("updated_at",)
