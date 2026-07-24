from django.contrib import admin
from .models import (
    AtsJudgeProfile,
    AppAutomationSettings,
    ApplicantProfile,
    ApplicationAttempt,
    ApplicationAttemptStep,
    AtsAutoSubmitStats,
    CustomerApiKey,
    LLMAppUsageTotals,
    LLMUsageByModel,
    LLMUsageByQuery,
    LLMProviderPreference,
    LLMProviderConfig,
    JobListing,
    JobMatchResult,
    JobListingEmbedding,
    Plan,
    SiteCredential,
    Subscription,
    StripeWebhookEvent,
    UsageCounter,
    UserDisqualifier,
    SavedJobSearch,
    UserExperienceSettings,
    OptimizerWorkflow,
    SystemPromptProfile,
    UserPromptProfile,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "slug",
        "name",
        "llm_requests_per_day",
        "job_searches_per_day",
        "apply_runs_per_day",
        "api_access",
        "is_default",
        "is_active",
        "stripe_price_id",
    )
    list_filter = ("is_active", "api_access", "is_default")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("owner", "plan", "status", "stripe_customer_id", "updated_at")
    list_filter = ("status", "plan")
    raw_id_fields = ("owner", "plan")
    search_fields = ("owner__username", "owner__email", "stripe_customer_id")


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    list_display = ("owner", "metric", "period_date", "count", "updated_at")
    list_filter = ("metric",)
    raw_id_fields = ("owner",)


@admin.register(CustomerApiKey)
class CustomerApiKeyAdmin(admin.ModelAdmin):
    list_display = ("owner", "prefix", "name", "created_at", "last_used_at", "revoked_at")
    raw_id_fields = ("owner",)
    search_fields = ("owner__username", "prefix", "name")


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "processed_at")
    search_fields = ("event_id", "event_type")


@admin.register(SavedJobSearch)
class SavedJobSearchAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "owner", "search_term", "location", "updated_at")
    list_filter = ("slug",)
    search_fields = ("name", "search_term", "slug", "owner__username")
    raw_id_fields = ("owner", "resume")


@admin.register(UserExperienceSettings)
class UserExperienceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "experience_mode",
        "email_verified_at",
        "pending_email",
        "onboarding_completed_at",
        "onboarding_dismissed_at",
        "step_llm_connected",
        "step_resume_uploaded",
        "step_first_search",
    )
    list_filter = ("experience_mode",)
    search_fields = ("owner__username", "owner__email", "pending_email")


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
        "is_local",
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
    list_display = ("title", "company_name", "source", "posted_at", "fetched_at")
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


@admin.register(AtsJudgeProfile)
class AtsJudgeProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_default", "is_builtin", "updated_at")
    list_filter = ("is_builtin", "is_default")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(OptimizerWorkflow)
class OptimizerWorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "ats_judge_profile", "max_iterations", "score_threshold", "updated_at")
    list_filter = ("max_iterations",)
    raw_id_fields = ("owner",)


@admin.register(SystemPromptProfile)
class SystemPromptProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(UserPromptProfile)
class UserPromptProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(ApplicantProfile)
class ApplicantProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "full_name", "email", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(SiteCredential)
class SiteCredentialAdmin(admin.ModelAdmin):
    list_display = ("domain", "label", "username", "updated_at")
    search_fields = ("domain", "label")
    readonly_fields = ("created_at", "updated_at")


class ApplicationAttemptStepInline(admin.TabularInline):
    model = ApplicationAttemptStep
    extra = 0
    readonly_fields = ("step_name", "message", "screenshot_path", "created_at")
    can_delete = False


@admin.register(ApplicationAttempt)
class ApplicationAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "pipeline_entry", "status", "ats_type", "automation_mode", "error_code", "updated_at")
    list_filter = ("status", "ats_type", "automation_mode")
    readonly_fields = ("created_at", "updated_at", "started_at", "submitted_at", "last_heartbeat_at")
    inlines = (ApplicationAttemptStepInline,)


@admin.register(AtsAutoSubmitStats)
class AtsAutoSubmitStatsAdmin(admin.ModelAdmin):
    list_display = ("ats_type", "clean_submit_streak", "total_submits", "total_corrections", "full_auto_enabled")
    readonly_fields = ("updated_at",)
