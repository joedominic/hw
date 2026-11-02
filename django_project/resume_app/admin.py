from django.contrib import admin
from .models import (
    AtsJudgeProfile,
    AppAutomationSettings,
    ApplicantProfile,
    CustomerApiKey,
    LLMAppUsageTotals,
    LLMUsageByModel,
    LLMUsageByQuery,
    LLMDailyUsageBreakdown,
    LLMProviderPreference,
    LLMProviderConfig,
    TenantPromptModelPreference,
    JobListing,
    JobMatchResult,
    JobListingEmbedding,
    Plan,
    Subscription,
    StripeWebhookEvent,
    UsageCounter,
    UserDisqualifier,
    SavedJobSearch,
    UserExperienceSettings,
    OptimizerWorkflow,
    SystemPromptProfile,
    UserPromptProfile,
    EmployerInterviewEvent,
    PipelineEntry,
    UserResume,
    OptimizedResume,
)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "slug",
        "name",
        "price_display",
        "llm_requests_per_day",
        "llm_tokens_per_day",
        "job_searches_per_day",
        "storage_mb",
        "api_access",
        "byok_required_for_completion",
        "is_default",
        "is_active",
        "stripe_price_id",
    )
    list_filter = ("is_active", "api_access", "byok_required_for_completion", "is_default")
    search_fields = ("slug", "name", "description", "stripe_price_id")
    prepopulated_fields = {"slug": ("name",)}
    actions = ["activate_plans", "deactivate_plans"]

    @admin.action(description="Mark selected plans as active")
    def activate_plans(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} plan(s) activated.")

    @admin.action(description="Mark selected plans as inactive")
    def deactivate_plans(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} plan(s) deactivated.")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "owner_email",
        "plan",
        "status",
        "stripe_customer_id",
        "trial_ends_at",
        "current_period_end",
        "updated_at",
    )
    list_filter = ("status", "plan", "created_at")
    raw_id_fields = ("owner", "plan")
    search_fields = (
        "owner__username",
        "owner__email",
        "stripe_customer_id",
        "stripe_subscription_id",
    )
    actions = [
        "assign_free_plan",
        "assign_byok_plan",
        "assign_pro_plan",
        "assign_power_plan",
        "mark_active",
        "mark_canceled",
        "reset_quotas_today",
    ]

    @admin.display(description="Email")
    def owner_email(self, obj):
        return obj.owner.email or "—"

    @admin.action(description="Assign selected subscriptions to Free plan")
    def assign_free_plan(self, request, queryset):
        from .subscriptions import assign_plan
        for sub in queryset:
            assign_plan(sub.owner, "free")
        self.message_user(request, f"Assigned {queryset.count()} subscriptions to Free plan.")

    @admin.action(description="Assign selected subscriptions to BYOK Rails plan")
    def assign_byok_plan(self, request, queryset):
        from .subscriptions import assign_plan
        for sub in queryset:
            assign_plan(sub.owner, "byok")
        self.message_user(request, f"Assigned {queryset.count()} subscriptions to BYOK Rails plan.")

    @admin.action(description="Assign selected subscriptions to Pro plan")
    def assign_pro_plan(self, request, queryset):
        from .subscriptions import assign_plan
        for sub in queryset:
            assign_plan(sub.owner, "pro")
        self.message_user(request, f"Assigned {queryset.count()} subscriptions to Pro plan.")

    @admin.action(description="Assign selected subscriptions to Power plan")
    def assign_power_plan(self, request, queryset):
        from .subscriptions import assign_plan
        for sub in queryset:
            assign_plan(sub.owner, "power")
        self.message_user(request, f"Assigned {queryset.count()} subscriptions to Power plan.")

    @admin.action(description="Mark selected subscriptions as Active")
    def mark_active(self, request, queryset):
        count = 0
        for sub in queryset:
            sub.status = Subscription.Status.ACTIVE
            sub.save(update_fields=["status", "past_due_since", "updated_at"])
            count += 1
        self.message_user(request, f"{count} subscriptions marked Active.")

    @admin.action(description="Mark selected subscriptions as Canceled")
    def mark_canceled(self, request, queryset):
        count = 0
        for sub in queryset:
            sub.status = Subscription.Status.CANCELED
            sub.save(update_fields=["status", "past_due_since", "updated_at"])
            count += 1
        self.message_user(request, f"{count} subscriptions marked Canceled.")

    @admin.action(description="Reset today's usage quotas for selected users")
    def reset_quotas_today(self, request, queryset):
        from django.utils import timezone
        user_ids = list(queryset.values_list("owner_id", flat=True))
        deleted, _ = UsageCounter.objects.filter(owner_id__in=user_ids, period_date=timezone.localdate()).delete()
        self.message_user(request, f"Reset {deleted} usage counters for {len(user_ids)} selected users.")


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
        "pipeline_match_score_min",
        "pipeline_purge_match_score_max",
        "pipeline_preference_margin_min",
        "vetting_to_applying_enabled",
        "vetting_interview_probability_min",
        "applying_optimizer_workflow",
        "cleanup_job_retention_days",
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


@admin.register(TenantPromptModelPreference)
class TenantPromptModelPreferenceAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "query_kind",
        "provider",
        "model",
        "is_active",
        "updated_at",
    )
    list_filter = ("query_kind", "provider", "is_active")
    search_fields = ("owner__username", "query_kind", "provider", "model")


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


@admin.register(LLMDailyUsageBreakdown)
class LLMDailyUsageBreakdownAdmin(admin.ModelAdmin):
    list_display = (
        "period_date",
        "query_kind",
        "provider",
        "model",
        "request_count",
        "sum_input_tokens",
        "sum_output_tokens",
        "last_used_at",
    )
    list_filter = ("period_date", "query_kind", "provider")


@admin.register(JobListing)
class JobListingAdmin(admin.ModelAdmin):
    list_display = ("title", "company_name", "source", "posted_at", "fetched_at")
    list_filter = ("source",)
    search_fields = ("title", "company_name")


@admin.register(JobMatchResult)
class JobMatchResultAdmin(admin.ModelAdmin):
    list_display = ("job_listing", "resume", "owner", "fit_score", "status", "analyzed_at")
    list_filter = ("status", "analyzed_at")
    list_select_related = ("owner", "job_listing", "resume")
    raw_id_fields = ("owner", "job_listing", "resume")
    search_fields = ("owner__username", "job_listing__title", "job_listing__company_name")


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


@admin.register(EmployerInterviewEvent)
class EmployerInterviewEventAdmin(admin.ModelAdmin):
    list_display = ("owner", "pipeline_entry", "round_type", "status", "scheduled_at", "interviewer_names")
    list_filter = ("round_type", "status")
    raw_id_fields = ("owner", "pipeline_entry")
    search_fields = ("interviewer_names", "location_or_link", "notes")


@admin.register(PipelineEntry)
class PipelineEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "job_listing", "stage", "post_apply_substatus", "track", "added_at", "removed_at")
    list_filter = ("stage", "post_apply_substatus", "track", "removed_at")
    list_select_related = ("owner", "job_listing", "search_profile")
    raw_id_fields = ("owner", "job_listing", "search_profile", "applied_optimized_resume")
    search_fields = ("owner__username", "job_listing__title", "job_listing__company_name", "track")
    date_hierarchy = "added_at"


@admin.register(UserResume)
class UserResumeAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "owner", "track", "is_library", "uploaded_at")
    list_filter = ("is_library", "uploaded_at")
    list_select_related = ("owner",)
    raw_id_fields = ("owner",)
    search_fields = ("original_filename", "track", "owner__username")


@admin.register(OptimizedResume)
class OptimizedResumeAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "status", "ats_score", "recruiter_score", "created_at")
    list_filter = ("status", "created_at")
    list_select_related = ("owner", "pipeline_entry")
    raw_id_fields = ("owner", "original_resume", "job_description", "pipeline_entry", "optimizer_workflow", "ats_judge_profile")
    search_fields = ("owner__username",)

