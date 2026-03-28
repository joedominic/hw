from django.contrib import admin
from .models import (
    AppAutomationSettings,
    LLMAppUsageTotals,
    LLMUsageByModel,
    LLMUsageByQuery,
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
        "cleanup_pipeline_retention_days",
        "cleanup_vetting_retention_days",
        "cleanup_applying_retention_days",
        "cleanup_done_retention_days",
        "stop_llm_requests",
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
    list_display = (
        "provider_config",
        "model",
        "priority",
        "rate_limit_rpm",
        "rate_limit_tpm",
        "rate_limit_cooldown_seconds",
        "updated_at",
    )
    list_filter = ("provider_config__provider",)


@admin.register(LLMAppUsageTotals)
class LLMAppUsageTotalsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "total_requests",
        "total_input_tokens",
        "total_output_tokens",
        "total_estimated_invokes",
        "updated_at",
    )


@admin.register(LLMUsageByModel)
class LLMUsageByModelAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "model",
        "request_count",
        "sum_input_tokens",
        "sum_output_tokens",
        "last_used_at",
    )
    list_filter = ("provider",)


@admin.register(LLMUsageByQuery)
class LLMUsageByQueryAdmin(admin.ModelAdmin):
    list_display = (
        "query_kind",
        "provider",
        "model",
        "request_count",
        "sum_input_tokens",
        "sum_output_tokens",
        "last_used_at",
    )
    list_filter = ("query_kind", "provider")


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
