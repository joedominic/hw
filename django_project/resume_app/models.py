from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from .tenancy import OwnedManager, user_resume_upload_to


class Track(models.Model):
    """
    Preference / search track, e.g. IC vs Management.

    slug is the stable identifier used in URL/query params and on related
    models (JobSearchTask.track, PipelineEntry.track, etc.).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tracks",
    )
    slug = models.SlugField(
        max_length=32,
        help_text="Short code used in URLs and tasks, e.g. 'ic', 'mgmt', 'eu_ic'.",
    )
    label = models.CharField(
        max_length=255,
        help_text="Human-friendly name, e.g. 'IC (Principal / Staff)'.",
    )
    description = models.TextField(blank=True)
    is_default = models.BooleanField(
        default=False,
        help_text="If true, used as fallback when no track is selected.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["slug"]
        unique_together = [("owner", "slug")]

    def __str__(self):
        return self.label or self.slug

    @classmethod
    def ensure_baseline(cls, user):
        """
        Ensure there is at least one track for this user.

        If no Track rows exist yet for the user, seed a single default search profile.
        """
        qs = cls.objects.for_user(user)
        if not qs.exists():
            cls.objects.get_or_create(
                owner=user,
                slug="general",
                defaults={"label": "General", "is_default": True},
            )
        elif not qs.filter(is_default=True).exists():
            first = qs.order_by("id").first()
            if first:
                qs.update(is_default=False)
                first.is_default = True
                first.save(update_fields=["is_default"])
        return qs.all()

    @classmethod
    def get_default_slug(cls, user) -> str:
        """Best-effort default track slug for a user."""
        qs = cls.objects.for_user(user)
        default = qs.filter(is_default=True).first()
        if default:
            return default.slug
        first = qs.order_by("id").first()
        if first:
            return first.slug
        obj, _created = cls.objects.get_or_create(
            owner=user,
            slug="general",
            defaults={"label": "General", "is_default": True},
        )
        return obj.slug


class UserResume(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="resumes",
    )
    file = models.FileField(upload_to=user_resume_upload_to)
    original_filename = models.CharField(max_length=255, blank=True, help_text="Original name of the uploaded file")
    track = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Preferred preference track slug for this resume (used to default Job Search track).",
    )
    is_library = models.BooleanField(
        default=False,
        help_text="True for PDFs uploaded on Resumes & Tracks; False for ephemeral optimizer run copies.",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        indexes = [
            models.Index(fields=["owner", "is_library", "-uploaded_at"]),
        ]

    def __str__(self):
        return self.original_filename or f"Resume {self.id} uploaded at {self.uploaded_at}"

    @classmethod
    def library(cls):
        """Resumes explicitly uploaded on the Resumes & Tracks page."""
        return cls.objects.filter(is_library=True)


class ResumeChunk(models.Model):
    """Local RAG index: one row per resume bullet/segment with dense embedding."""

    user_resume = models.ForeignKey(
        UserResume,
        on_delete=models.CASCADE,
        related_name="resume_chunks",
    )
    ordinal = models.PositiveIntegerField()
    section = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    embedding = models.JSONField(help_text="384-dim float list (sentence-transformers)")
    embedding_model = models.CharField(max_length=128)
    source_fingerprint = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["user_resume_id", "ordinal"]
        indexes = [
            models.Index(fields=["user_resume", "source_fingerprint"]),
        ]

    def __str__(self):
        return f"ResumeChunk ur={self.user_resume_id} #{self.ordinal}"


class JobDescription(models.Model):
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class OptimizedResume(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="optimized_resumes",
    )
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    original_resume = models.ForeignKey(UserResume, on_delete=models.CASCADE)
    job_description = models.ForeignKey(JobDescription, on_delete=models.CASCADE)
    optimized_content = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=255, default=STATUS_QUEUED, choices=STATUS_CHOICES)
    status_display = models.CharField(max_length=255, blank=True, help_text="Human-readable progress, e.g. 'Drafting iteration 1'")
    error_message = models.TextField(blank=True, null=True)
    ats_score = models.IntegerField(null=True, blank=True)
    recruiter_score = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    total_input_tokens = models.IntegerField(null=True, blank=True)
    total_output_tokens = models.IntegerField(null=True, blank=True)
    pipeline_entry = models.ForeignKey(
        "PipelineEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimized_resumes",
        help_text="Pipeline row this optimization was started for (Applying stage).",
    )
    optimizer_workflow = models.ForeignKey(
        "OptimizerWorkflow",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimized_resumes",
        help_text="Saved workflow used when this run was enqueued.",
    )
    ats_judge_profile = models.ForeignKey(
        "AtsJudgeProfile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimized_resumes",
        help_text="ATS judge prompt profile used for this optimization run.",
    )
    optimization_notes = models.TextField(
        blank=True,
        default="",
        help_text="Optional user notes passed to the Writer (token-capped).",
    )
    pipeline_skills_json = models.TextField(
        blank=True,
        default="",
        help_text="Optional JSON or keyword blob from pipeline (token-capped).",
    )
    job_highlights = models.TextField(
        blank=True,
        default="",
        help_text="Optional short job-specific bullets (token-capped).",
    )
    optimizer_context_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Last run: writer JD/resume budgets, retrieval stats (debug).",
    )
    cover_letter = models.TextField(
        blank=True,
        default="",
        help_text="On-demand generated cover letter for this job optimization.",
    )
    cover_letter_generated_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()


class AgentLog(models.Model):
    optimized_resume = models.ForeignKey(OptimizedResume, related_name='logs', on_delete=models.CASCADE)
    step_name = models.CharField(max_length=100)
    thought = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class AtsJudgeProfile(models.Model):
    """
    Named ATS judge prompt (system/user/legacy triple). Runtime uses global rows
    (owner=null) managed by staff. Users may select one per optimizer run;
    OptimizerWorkflow may set a default profile for that workflow.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ats_judge_profiles",
        help_text="Null for system-wide admin-managed profiles. Legacy per-user rows are ignored at runtime.",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(
        max_length=64,
        help_text="Stable identifier for API and defaults (e.g. 'default').",
    )
    ats_judge = models.TextField(blank=True)
    ats_judge_system = models.TextField(blank=True)
    ats_judge_user = models.TextField(blank=True)
    is_builtin = models.BooleanField(
        default=False,
        help_text="Seeded built-in profile; deletion may be restricted in UI.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Global fallback when no per-run or workflow ATS is selected.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["name"]
        verbose_name = "ATS judge profile"
        verbose_name_plural = "ATS judge profiles"
        unique_together = [("owner", "slug")]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not (self.slug or "").strip():
            base = slugify(self.name) or "ats-profile"
            candidate = base
            n = 2
            while (
                AtsJudgeProfile.objects.filter(slug=candidate, owner=self.owner)
                .exclude(pk=self.pk)
                .exists()
            ):
                candidate = f"{base}-{n}"
                n += 1
            self.slug = candidate
        if self.is_default:
            AtsJudgeProfile.objects.filter(owner=self.owner).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class SystemPromptProfile(models.Model):
    """
    Singleton system-wide prompt overrides managed by staff/admins.
    Empty string for a field means fall back to the code default in prompts.py.
    All users share this one set of prompts.
    """

    writer = models.TextField(blank=True)
    writer_system = models.TextField(blank=True)
    writer_user = models.TextField(blank=True)
    ats_judge = models.TextField(blank=True)
    ats_judge_system = models.TextField(blank=True)
    ats_judge_user = models.TextField(blank=True)
    recruiter_judge = models.TextField(blank=True)
    recruiter_judge_system = models.TextField(blank=True)
    recruiter_judge_user = models.TextField(blank=True)
    matching = models.TextField(blank=True)
    matching_system = models.TextField(blank=True)
    matching_user = models.TextField(blank=True)
    insights = models.TextField(blank=True)
    insights_system = models.TextField(blank=True)
    insights_user = models.TextField(blank=True)
    jd_cleanse = models.TextField(blank=True)
    jd_cleanse_system = models.TextField(blank=True)
    jd_cleanse_user = models.TextField(blank=True)
    cover_letter = models.TextField(blank=True)
    cover_letter_system = models.TextField(blank=True)
    cover_letter_user = models.TextField(blank=True)
    interview_prep = models.TextField(blank=True)
    interview_prep_system = models.TextField(blank=True)
    interview_prep_user = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System prompt profile"
        verbose_name_plural = "System prompt profiles"

    def __str__(self) -> str:
        return "System prompt profile"

    @classmethod
    def get_solo(cls) -> "SystemPromptProfile":
        """Return the singleton row (pk=1), creating it if needed."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class UserPromptProfile(models.Model):
    """
    Legacy per-user prompt overrides. No longer used at runtime; retained for
    historical data. Prefer SystemPromptProfile.
    """

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="prompt_profile",
    )
    writer = models.TextField(blank=True)
    writer_system = models.TextField(blank=True)
    writer_user = models.TextField(blank=True)
    ats_judge = models.TextField(blank=True)
    ats_judge_system = models.TextField(blank=True)
    ats_judge_user = models.TextField(blank=True)
    recruiter_judge = models.TextField(blank=True)
    recruiter_judge_system = models.TextField(blank=True)
    recruiter_judge_user = models.TextField(blank=True)
    matching = models.TextField(blank=True)
    matching_system = models.TextField(blank=True)
    matching_user = models.TextField(blank=True)
    insights = models.TextField(blank=True)
    insights_system = models.TextField(blank=True)
    insights_user = models.TextField(blank=True)
    jd_cleanse = models.TextField(blank=True)
    jd_cleanse_system = models.TextField(blank=True)
    jd_cleanse_user = models.TextField(blank=True)
    cover_letter = models.TextField(blank=True)
    cover_letter_system = models.TextField(blank=True)
    cover_letter_user = models.TextField(blank=True)
    interview_prep = models.TextField(blank=True)
    interview_prep_system = models.TextField(blank=True)
    interview_prep_user = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User prompt profile"
        verbose_name_plural = "User prompt profiles"

    def __str__(self):
        return f"Prompt profile ({self.owner_id})"

    @classmethod
    def get_for_user(cls, user):
        obj, _created = cls.objects.get_or_create(owner=user)
        return obj


class LLMProviderConfig(models.Model):
    """Stored API key and default model per provider. Key is encrypted at rest."""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_provider_configs",
    )
    provider = models.CharField(max_length=64)
    encrypted_api_key = models.TextField(blank=True)
    default_model = models.CharField(max_length=128, blank=True)
    is_active = models.BooleanField(
        default=False,
        help_text="DB-backed active provider for background jobs (Huey) and web flows.",
    )
    priority = models.PositiveSmallIntegerField(
        default=100,
        help_text="Lower number = higher preference when active provider is unavailable.",
    )
    last_validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        verbose_name = "LLM Provider Config"
        verbose_name_plural = "LLM Provider Configs"
        unique_together = [("owner", "provider")]

    def has_key(self) -> bool:
        return bool((self.encrypted_api_key or "").strip())


class LLMProviderPreference(models.Model):
    """
    Ordered provider/model runtime preference rows.
    Multiple rows can point to the same provider with different models.
    """

    provider_config = models.ForeignKey(
        LLMProviderConfig,
        on_delete=models.CASCADE,
        related_name="preference_rows",
    )
    model = models.CharField(max_length=128, blank=True)
    is_local = models.BooleanField(
        default=False,
        help_text="If true, this provider+model is treated as local (prioritized or required).",
    )
    priority = models.PositiveSmallIntegerField(default=100)
    rate_limit_rpm = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional max requests per minute for this provider+model row. Empty = use env/provider defaults or no limit.",
    )
    rate_limit_tpm = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional max input tokens per minute (estimated) for this row. Empty = use env/provider defaults or no limit.",
    )
    rate_limit_cooldown_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="After rate limit or 429, skip this provider+model for this many seconds (empty = 300).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.provider_config.provider} / {self.model or '(default)'} @ {self.priority}"


class LLMAppUsageTotals(models.Model):
    """Per-user aggregate LLM token usage."""

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_usage_totals",
    )
    total_input_tokens = models.BigIntegerField(default=0)
    total_output_tokens = models.BigIntegerField(default=0)
    total_requests = models.PositiveIntegerField(default=0)
    total_estimated_invokes = models.PositiveIntegerField(
        default=0,
        help_text="Calls where tokens were heuristic (provider did not report usage).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "LLM app usage totals"
        verbose_name_plural = "LLM app usage totals"

    def __str__(self):
        return "LLM usage totals"

    @classmethod
    def get_for_user(cls, user):
        obj, _created = cls.objects.get_or_create(owner=user)
        return obj


class LLMUsageByModel(models.Model):
    """Per provider + model usage counters (normalized model key)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_usage_by_model",
    )
    provider = models.CharField(max_length=64, db_index=True)
    model = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Resolved model name, or __default__ when empty.",
    )
    request_count = models.PositiveIntegerField(default=0)
    sum_input_tokens = models.BigIntegerField(default=0)
    sum_output_tokens = models.BigIntegerField(default=0)
    sum_cached_tokens = models.BigIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()

    class Meta:
        verbose_name = "LLM usage by model"
        verbose_name_plural = "LLM usage by model"
        unique_together = [("owner", "provider", "model")]

    def __str__(self):
        return f"{self.provider} / {self.model}"


class LLMUsageByQuery(models.Model):
    """Per logical LLM use-case, provider, and model (gateway-recorded only)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="llm_usage_by_query",
    )
    query_kind = models.CharField(max_length=64, db_index=True)
    provider = models.CharField(max_length=64, db_index=True)
    model = models.CharField(
        max_length=128,
        db_index=True,
        help_text="Resolved model name, or __default__ when empty.",
    )
    request_count = models.PositiveIntegerField(default=0)
    sum_input_tokens = models.BigIntegerField(default=0)
    sum_output_tokens = models.BigIntegerField(default=0)
    sum_cached_tokens = models.BigIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()

    class Meta:
        verbose_name = "LLM usage by query"
        verbose_name_plural = "LLM usage by query"
        unique_together = [("owner", "query_kind", "provider", "model")]

    def __str__(self):
        return f"{self.query_kind} / {self.provider} / {self.model}"


class AppAutomationSettings(models.Model):
    """
    Per-user pipeline / vetting automation thresholds.
    """

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="automation_settings",
    )
    pipeline_to_vetting_enabled = models.BooleanField(default=False)
    pipeline_preference_margin_min = models.IntegerField(
        default=0,
        help_text="Promote Pipeline → Vetting when Pref margin (same as pipeline badge) is >= this value.",
    )
    vetting_to_applying_enabled = models.BooleanField(default=False)
    vetting_interview_probability_min = models.PositiveSmallIntegerField(
        default=70,
        help_text="Promote Vetting → Applying when interview probability is >= this (0–100).",
    )
    applying_optimizer_workflow = models.ForeignKey(
        "OptimizerWorkflow",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Default Resume Optimizer workflow for Applying-stage runs (auto + bulk).",
    )
    stop_llm_requests = models.BooleanField(
        default=False,
        help_text="When set, the app will not send any LLM API requests (kill switch).",
    )
    cleanup_pipeline_retention_days = models.PositiveSmallIntegerField(
        default=2,
        help_text="Cleanup Manager: remove Pipeline-stage rows older than this many days (0 = off).",
    )
    cleanup_vetting_retention_days = models.PositiveSmallIntegerField(
        default=6,
        help_text="Cleanup Manager: remove Vetting-stage rows older than this many days (0 = off).",
    )
    cleanup_applying_retention_days = models.PositiveSmallIntegerField(
        default=10,
        help_text="Cleanup Manager: remove Applying-stage rows older than this many days (0 = off).",
    )
    cleanup_done_retention_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="Cleanup Manager: remove Done-stage rows older than this many days (0 = off).",
    )
    cleanup_generated_resume_retention_days = models.PositiveSmallIntegerField(
        default=7,
        help_text="Remove optimizer ephemeral resume PDFs older than this many days (0 = off).",
    )
    default_optimization_notes = models.TextField(
        blank=True,
        default="",
        help_text="Default Writer notes for the Resume Optimizer (Step 2 / Settings).",
    )
    default_pipeline_skills_json = models.TextField(
        blank=True,
        default="",
        help_text="Default pipeline/skills JSON for the Resume Optimizer.",
    )
    default_job_highlights = models.TextField(
        blank=True,
        default="",
        help_text="Default supplemental accomplishments for the Resume Optimizer.",
    )

    # --- Autonomous Apply Agent ---
    apply_agent_enabled = models.BooleanField(
        default=False,
        help_text="Master switch for the Autonomous Apply Agent (browser automation).",
    )
    apply_automation_mode = models.CharField(
        max_length=16,
        choices=[("semi_auto", "Semi-auto (review before submit)"), ("full_auto", "Full-auto (submit without review)")],
        default="semi_auto",
        help_text="Semi-auto pauses for human approval before submitting; full-auto submits autonomously for graduated ATS adapters only.",
    )
    apply_min_optimizer_score = models.PositiveSmallIntegerField(
        default=0,
        help_text="Require optimized resume avg (ATS+recruiter) >= this before applying (0 = no gate).",
    )
    apply_allowed_ats = models.JSONField(
        default=list,
        blank=True,
        help_text="ATS slugs the agent is allowed to apply to (e.g. ['greenhouse', 'lever']). Empty = greenhouse + lever defaults.",
    )
    apply_generic_fallback_enabled = models.BooleanField(
        default=True,
        help_text="Use the browser-use generic agent for unknown ATS (always requires review; never auto-submits).",
    )
    apply_resume_upload_format = models.CharField(
        max_length=8,
        choices=[("pdf", "PDF"), ("docx", "Word (DOCX)")],
        default="pdf",
        help_text="File format used when the agent uploads the optimized resume. PDF is the stable default for v1.",
    )
    apply_full_auto_min_clean_submits = models.PositiveSmallIntegerField(
        default=10,
        help_text="An ATS may graduate to full-auto only after this many approved submissions with zero human corrections.",
    )
    apply_agent_llm_provider = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Dedicated LLM provider for browser-use generic form fill. Blank = global active provider.",
    )
    apply_agent_llm_model = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Model for the apply-agent LLM. Blank = provider default from Settings.",
    )
    apply_browser_show_window = models.BooleanField(
        default=False,
        help_text="When True, show a visible Chromium window during apply-agent browser steps (dev; requires Huey on this machine).",
    )
    export_replacements = models.JSONField(
        default=list,
        blank=True,
        help_text="Up to 5 {token, value} pairs applied when exporting optimized resumes to PDF/Word.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "App automation settings"
        verbose_name_plural = "App automation settings"

    def __str__(self):
        return "App automation settings"

    @classmethod
    def get_for_user(cls, user):
        obj, _created = cls.objects.get_or_create(
            owner=user,
            defaults={
                "pipeline_to_vetting_enabled": False,
                "pipeline_preference_margin_min": 0,
                "vetting_to_applying_enabled": False,
                "vetting_interview_probability_min": 70,
                "stop_llm_requests": False,
                "cleanup_pipeline_retention_days": 2,
                "cleanup_vetting_retention_days": 6,
                "cleanup_applying_retention_days": 10,
                "cleanup_done_retention_days": 0,
                "cleanup_generated_resume_retention_days": 7,
            },
        )
        return obj

    @staticmethod
    def normalize_export_replacements(raw) -> list:
        """Return exactly 5 {token, value} dicts for Settings UI / export."""
        out: list[dict] = []
        if isinstance(raw, list):
            for entry in raw[:5]:
                if isinstance(entry, dict):
                    out.append(
                        {
                            "token": (entry.get("token") or "").strip(),
                            "value": "" if entry.get("value") is None else str(entry.get("value")),
                        }
                    )
                else:
                    out.append({"token": "", "value": ""})
        while len(out) < 5:
            out.append({"token": "", "value": ""})
        return out[:5]

    def set_export_replacements(self, raw) -> list:
        """Persist normalized replacements and return the stored list."""
        normalized = self.normalize_export_replacements(raw)
        self.export_replacements = normalized
        self.save(update_fields=["export_replacements", "updated_at"])
        return normalized


class JobListing(models.Model):
    """A job fetched from a source (e.g. JobSpy Indeed). Deduplicated by (source, external_id)."""
    source = models.CharField(max_length=64)  # e.g. jobspy_indeed, jobspy_linkedin
    external_id = models.CharField(max_length=256)
    title = models.CharField(max_length=512)
    company_name = models.CharField(max_length=512)
    location = models.CharField(max_length=512, blank=True)
    description = models.TextField(blank=True)
    url = models.URLField(max_length=2048, blank=True)
    posted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the job was posted on the source board (from JobSpy date_posted).",
    )
    fetched_at = models.DateTimeField(auto_now_add=True)
    raw_json = models.JSONField(null=True, blank=True)
    # Cached preference metrics for pipeline/saved jobs (per track) live in JobListingTrackMetrics.
    pipeline_last_scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("source", "external_id")]
        ordering = ["-fetched_at"]

    def __str__(self):
        return f"{self.title} @ {self.company_name}"


class JobListingAction(models.Model):
    """User actions on job listings: liked, disliked, hidden, saved."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_listing_actions",
    )
    class ActionType(models.TextChoices):
        LIKED = "liked", "Liked"
        DISLIKED = "disliked", "Disliked"
        HIDDEN = "hidden", "Hidden"
        SAVED = "saved", "Saved"

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    action = models.CharField(max_length=16, choices=ActionType.choices)
    search_profile = models.ForeignKey(
        "SearchProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="profile_listing_actions",
    )
    track = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Legacy track slug mirror; kept in sync with search_profile.slug.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "job_listing", "action", "track")]


class JobListingEmbedding(models.Model):
    class EmbeddingType(models.TextChoices):
        LIKED = "liked", "Liked"
        DISLIKED = "disliked", "Disliked"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_listing_embeddings",
    )
    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    embedding_type = models.CharField(
        max_length=16,
        choices=EmbeddingType.choices,
        default=EmbeddingType.LIKED,
    )
    search_profile = models.ForeignKey(
        "SearchProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="profile_embeddings",
    )
    track = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Legacy track slug mirror; kept in sync with search_profile.slug.",
    )
    embedding = models.JSONField(help_text="List of floats, e.g. 384-dim from sentence-transformers")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "job_listing", "embedding_type", "track")]


class JobListingTrackMetrics(models.Model):
    """
    Cached per-track preference metrics for pipeline / saved jobs.

    One row per (job_listing, track) so we can support arbitrary tracks
    beyond the original IC / Management split.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_listing_track_metrics",
    )
    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    search_profile = models.ForeignKey(
        "SearchProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="profile_metrics",
    )
    track = models.CharField(
        max_length=32,
        help_text="Legacy track slug mirror; kept in sync with search_profile.slug.",
    )
    focus_percent = models.IntegerField(null=True, blank=True)
    focus_after_penalty = models.IntegerField(null=True, blank=True)
    preference_margin = models.IntegerField(null=True, blank=True)
    last_scored_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "job_listing", "track")]
        indexes = [
            models.Index(fields=["owner", "track", "job_listing"]),
        ]

def _normalize_disqualifier_phrase(phrase: str) -> str:
    """Lowercase, strip, collapse whitespace for uniqueness."""
    if not phrase or not isinstance(phrase, str):
        return ""
    return " ".join((phrase or "").lower().strip().split())


class UserDisqualifier(models.Model):
    """
    Words or phrases the user wants to avoid in job descriptions.
    """
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="disqualifiers",
    )
    phrase = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["phrase"]
        unique_together = [("owner", "phrase")]

    def save(self, *args, **kwargs):
        self.phrase = _normalize_disqualifier_phrase(self.phrase)
        super().save(*args, **kwargs)


class OptimizerWorkflow(models.Model):
    """
    Saved custom workflow for Resume Optimization.

    Runtime uses global rows (owner=null) managed by staff. All subscribers
    can select them in the Optimizer; legacy per-user rows are ignored.
    """
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="optimizer_workflows",
        help_text="Null for system-wide admin-managed workflows. Legacy per-user rows are ignored at runtime.",
    )
    name = models.CharField(max_length=255)
    steps = models.JSONField(
        help_text="Ordered list of step ids, e.g. ['writer', 'ats_judge', 'recruiter_judge']"
    )
    loop_to = models.CharField(max_length=64, blank=True)
    max_iterations = models.PositiveSmallIntegerField(default=3)
    score_threshold = models.PositiveSmallIntegerField(default=85)
    ats_judge_profile = models.ForeignKey(
        AtsJudgeProfile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimizer_workflows",
        help_text="Default ATS judge prompt when this workflow is used.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class JobMatchResult(models.Model):
    """Fit-check result for a (job_listing, resume) pair. One per job per resume per user."""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_match_results",
    )
    STATUS_ANALYZED = "analyzed"
    STATUS_APPLIED = "applied"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_ANALYZED, "Analyzed"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    resume = models.ForeignKey(UserResume, on_delete=models.CASCADE)
    fit_score = models.IntegerField(null=True, blank=True)  # 0-100
    reasoning = models.TextField(blank=True)
    analyzed_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=32, default=STATUS_ANALYZED, choices=STATUS_CHOICES)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "job_listing", "resume")]
        ordering = ["-analyzed_at"]

    def __str__(self):
        return f"Match {self.job_listing_id} x resume {self.resume_id} ({self.fit_score})"


class SearchProfile(models.Model):
    """
    Atomic job-search context: query config, pipeline scope, and (future) schedule.
    Replaces the split between SavedJobSearch + Track slug for normal workflows.
    """

    SCHEDULE_OFF = "off"
    SCHEDULE_DAILY = "daily"
    SCHEDULE_WEEKDAYS = "weekdays"
    SCHEDULE_WEEKLY = "weekly"
    SCHEDULE_CUSTOM = "custom"
    SCHEDULE_INTERVAL_CHOICES = [
        (SCHEDULE_OFF, "Off"),
        (SCHEDULE_DAILY, "Daily"),
        (SCHEDULE_WEEKDAYS, "Weekdays"),
        (SCHEDULE_WEEKLY, "Weekly"),
        (SCHEDULE_CUSTOM, "Custom"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="search_profiles",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(
        max_length=32,
        help_text="URL-safe profile key (unique per owner).",
    )
    description = models.TextField(blank=True, default="")
    search_term = models.CharField(max_length=512)
    location = models.CharField(max_length=512, blank=True, default="")
    profile_slug = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Deprecated: legacy track slug mirror; kept in sync with slug during migration.",
    )
    resume = models.ForeignKey(
        "UserResume",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="search_profiles",
    )
    min_score = models.PositiveSmallIntegerField(null=True, blank=True)
    results_wanted = models.PositiveSmallIntegerField(default=50)
    site_names = models.JSONField(default=list, blank=True)
    llm_model = models.CharField(max_length=128, blank=True, default="")
    schedule_interval = models.CharField(
        max_length=16,
        choices=SCHEDULE_INTERVAL_CHOICES,
        default=SCHEDULE_OFF,
    )
    schedule_time = models.TimeField(null=True, blank=True)
    schedule_cron = models.CharField(max_length=128, blank=True, default="")
    schedule_is_active = models.BooleanField(default=False)
    schedule_next_run_at = models.DateTimeField(null=True, blank=True)
    is_default = models.BooleanField(
        default=False,
        help_text="At most one default profile per owner (legacy General bucket).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["-updated_at", "name"]
        unique_together = [("owner", "name"), ("owner", "slug")]
        verbose_name = "Search profile"
        verbose_name_plural = "Search profiles"

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        slug = (self.slug or self.profile_slug or "").strip().lower()
        if slug:
            self.slug = slug
            self.profile_slug = slug
        super().save(*args, **kwargs)

    def to_query_params(self) -> dict[str, str]:
        """Build GET query params for jobs_search from this profile."""
        params: dict[str, str] = {}
        if self.search_term:
            params["q"] = self.search_term
        if self.location:
            params["location"] = self.location
        if self.slug:
            params["profile"] = self.slug
            params["track"] = self.slug
        if self.resume_id:
            params["resume_id"] = str(self.resume_id)
        if self.min_score is not None:
            params["min_score"] = str(self.min_score)
        if self.results_wanted:
            params["results_wanted"] = str(self.results_wanted)
        if self.llm_model:
            params["llm_model"] = self.llm_model
        for site in self.site_names or []:
            params.setdefault("site_name", [])
            if isinstance(params["site_name"], list):
                params["site_name"].append(site)
        return params


# Backward-compatible alias during consolidation.
SavedJobSearch = SearchProfile


class JobSearchTask(models.Model):
    """Scheduled job search: runs on cron, accumulates results into pipeline by track."""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_search_tasks",
    )
    saved_search = models.OneToOneField(
        "SearchProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="scheduled_task",
        help_text="When set, this task runs the linked search profile configuration.",
    )
    name = models.CharField(max_length=255, blank=True, help_text="Optional label for this task")
    search_term = models.CharField(max_length=512)
    location = models.CharField(max_length=512, blank=True)
    track = models.CharField(
        max_length=32,
        default="ic",
        help_text="Track slug this task feeds, e.g. 'ic', 'mgmt', or a custom track.",
    )
    jobs_to_fetch = models.PositiveIntegerField(default=50)
    site_name = models.JSONField(default=list, help_text="e.g. ['indeed']")
    frequency = models.CharField(
        max_length=128,
        help_text="Cron expression, e.g. '0 9 * * *' for daily 9am. Validated on save.",
    )
    start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="Time of day for first run and staggering so tasks don't overlap.",
    )
    is_active = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["name", "id"]

    def clean(self):
        super().clean()
        if self.frequency:
            try:
                import croniter
                croniter.croniter(self.frequency)
            except Exception as e:
                from django.core.exceptions import ValidationError
                raise ValidationError({"frequency": f"Invalid cron expression: expected 5 fields (minute hour day month weekday). {e}"})

    def __str__(self):
        return self.name or f"{self.search_term} ({self.track})"


class PipelineEntry(models.Model):
    """Links a JobListing to a track for pipeline display. removed_at = soft-deleted (task won't re-add)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pipeline_entries",
    )
    class Stage(models.TextChoices):
        PIPELINE = "pipeline", "Pipeline"
        VETTING = "vetting", "Vetting"
        APPLYING = "applying", "Applying"
        DONE = "done", "Done"
        DELETED = "deleted", "Deleted"

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    search_profile = models.ForeignKey(
        "SearchProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pipeline_entries",
        help_text="Search profile scope (Phase 1 dual-write with track slug).",
    )
    track = models.CharField(
        max_length=32,
        help_text="Track slug for this pipeline row (legacy; mirrors search_profile.slug).",
    )
    stage = models.CharField(
        max_length=16,
        choices=Stage.choices,
        blank=True,
        default="",
        help_text="Lightweight stage for this job in the pipeline (e.g. vetting, applying, done). Blank means legacy/default pipeline.",
    )
    added_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    # Vetting stage: interview probability + short explanation derived from Matching prompt.
    # Persisted so the UI can show badges without waiting for a live LLM call.
    vetting_interview_probability = models.IntegerField(null=True, blank=True)
    vetting_interview_reasoning = models.TextField(null=True, blank=True)
    vetting_interview_resume_id = models.IntegerField(null=True, blank=True)
    vetting_interview_scored_at = models.DateTimeField(null=True, blank=True)
    interview_prep = models.TextField(
        blank=True,
        default="",
        help_text="On-demand generated interview prep (JSON or markdown) for Done-stage jobs.",
    )
    interview_prep_generated_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "job_listing", "track")]
        ordering = ["-added_at"]

    def __str__(self):
        return f"{self.job_listing_id} @ {self.track}"

    def move_to_pipeline(self, save: bool = True):
        self.stage = self.Stage.PIPELINE
        if save:
            self.save(update_fields=["stage"])

    def move_to_vetting(self, save: bool = True):
        # Allow moving into vetting only from blank/default or pipeline stage.
        if self.stage not in ("", self.Stage.PIPELINE):
            return
        self.stage = self.Stage.VETTING
        if save:
            self.save(update_fields=["stage"])

    def move_to_applying(self, save: bool = True):
        # Allow moving into applying only from vetting.
        if self.stage not in ("", self.Stage.PIPELINE, self.Stage.VETTING):
            return
        self.stage = self.Stage.APPLYING
        if save:
            self.save(update_fields=["stage"])

    def mark_done(self, save: bool = True):
        # Mark as fully applied.
        self.stage = self.Stage.DONE
        if save:
            self.save(update_fields=["stage"])

    def mark_deleted(self, save: bool = True):
        # Move to deleted stage and set removed_at so background tasks won't re-add.
        self.stage = self.Stage.DELETED
        if self.removed_at is None:
            self.removed_at = timezone.now()
            update_fields = ["stage", "removed_at"]
        else:
            update_fields = ["stage"]
        if save:
            self.save(update_fields=update_fields)


class JobSearchTaskRun(models.Model):
    """One run of a JobSearchTask: summary counts and status."""
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    task = models.ForeignKey(JobSearchTask, on_delete=models.CASCADE, related_name="runs")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default=STATUS_RUNNING, choices=STATUS_CHOICES)
    jobs_fetched = models.PositiveIntegerField(default=0)
    jobs_after_filter = models.PositiveIntegerField(default=0)
    jobs_added_to_pipeline = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.task_id} @ {self.started_at}"


class ApplicantProfile(models.Model):
    """
    Per-user applicant personal data used to fill job application forms.
    """

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="applicant_profile",
    )
    full_name = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=64, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    linkedin_url = models.URLField(max_length=512, blank=True, default="")
    website_url = models.URLField(max_length=512, blank=True, default="")
    work_authorization = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="e.g. 'US Citizen', 'Green Card', 'H-1B'. Used for work-eligibility questions.",
    )
    requires_sponsorship = models.BooleanField(
        default=False,
        help_text="Answer to 'Do you require sponsorship?' style questions.",
    )
    salary_expectation = models.CharField(max_length=128, blank=True, default="")
    cover_letter_template = models.TextField(
        blank=True,
        default="",
        help_text="Optional default cover letter / message body. Placeholders like {company} and {title} are substituted.",
    )
    custom_qa_pairs = models.JSONField(
        default=dict,
        blank=True,
        help_text="Key/value answers for edge-case questions (e.g. {'GitHub URL': '...', 'Years with Django': '5'}).",
    )
    include_eeo = models.BooleanField(
        default=False,
        help_text="If false, the agent leaves voluntary EEO/demographic questions blank.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Applicant profile"
        verbose_name_plural = "Applicant profile"

    def __str__(self):
        return self.full_name or "Applicant profile"

    @classmethod
    def get_for_user(cls, user):
        obj, _created = cls.objects.get_or_create(owner=user)
        return obj


class SiteCredential(models.Model):
    """
    Encrypted login credentials and/or saved session cookies for a career-site
    domain or ATS. Reused across application attempts so the agent does not have
    to create accounts repeatedly. Secrets are encrypted at rest (Fernet).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="site_credentials",
    )
    domain = models.CharField(
        max_length=255,
        help_text="Host the credential applies to, e.g. 'boards.greenhouse.io' or 'acme.com'.",
    )
    label = models.CharField(max_length=255, blank=True, default="")
    username = models.CharField(max_length=255, blank=True, default="")
    encrypted_password = models.TextField(blank=True, default="")
    encrypted_session_cookies = models.TextField(
        blank=True,
        default="",
        help_text="Encrypted JSON blob of cookies for explicit injection into a fresh browser context.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["domain"]
        unique_together = [("owner", "domain")]

    def __str__(self):
        return self.label or self.domain

    def set_password(self, plain: str) -> None:
        from .crypto import encrypt_api_key

        self.encrypted_password = encrypt_api_key(plain or "")

    def get_password(self) -> str:
        from .crypto import decrypt_api_key

        return decrypt_api_key(self.encrypted_password or "")

    def set_session_cookies(self, cookies: list | dict) -> None:
        import json

        from .crypto import encrypt_api_key

        self.encrypted_session_cookies = encrypt_api_key(json.dumps(cookies or []))

    def get_session_cookies(self) -> list:
        import json

        from .crypto import decrypt_api_key

        raw = decrypt_api_key(self.encrypted_session_cookies or "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return data if isinstance(data, list) else []


class ApplicationAttempt(models.Model):
    """
    One run of the Autonomous Apply Agent for a single Applying-stage pipeline entry.

    The agent advances this row through a state machine (see Status). Every step
    persists resumable context to the DB so stateless Huey workers can resume after
    a restart; the browser session itself is never kept alive between tasks.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        OPTIMIZING = "optimizing", "Optimizing resume"
        WAITING_OPTIMIZER = "waiting_optimizer", "Waiting for optimizer"
        RESOLVE_AND_DETECT = "resolve_and_detect", "Resolving URL"
        DRY_RUN_FILL = "dry_run_fill", "Dry-run fill"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
        SUBMITTING = "submitting", "Submitting"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    # Non-terminal statuses the heartbeat sweeper should keep nudging forward.
    ACTIVE_STATUSES = (
        Status.QUEUED,
        Status.OPTIMIZING,
        Status.WAITING_OPTIMIZER,
        Status.RESOLVE_AND_DETECT,
        Status.DRY_RUN_FILL,
        Status.SUBMITTING,
    )

    class Mode(models.TextChoices):
        SEMI_AUTO = "semi_auto", "Semi-auto"
        FULL_AUTO = "full_auto", "Full-auto"

    # Error codes for failed attempts (drives UI messaging).
    ERROR_AUTOMATION_TIMEOUT = "automation_timeout"
    ERROR_SUBMIT_AMBIGUOUS = "submit_ambiguous"
    ERROR_UNRESOLVED_URL = "unresolved_url"
    ERROR_CAPTCHA = "captcha"
    ERROR_NO_RESUME = "no_resume"
    ERROR_NO_ADAPTER = "no_adapter"
    ERROR_FILL_FAILED = "fill_failed"
    ERROR_REJECTED = "rejected"
    ERROR_OPTIMIZER_FAILED = "optimizer_failed"

    pipeline_entry = models.ForeignKey(
        "PipelineEntry",
        on_delete=models.CASCADE,
        related_name="application_attempts",
    )
    optimized_resume = models.ForeignKey(
        "OptimizedResume",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="application_attempts",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    automation_mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.SEMI_AUTO)
    apply_url = models.URLField(max_length=2048, blank=True, default="")
    ats_type = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Detected ATS slug, e.g. greenhouse, lever, workday, unknown.",
    )
    confidence = models.FloatField(
        null=True,
        blank=True,
        help_text="Adapter/agent confidence (0.0–1.0) that the form was filled correctly.",
    )
    fill_payload_json = models.JSONField(
        null=True,
        blank=True,
        help_text="Semantic answer key captured in the dry run (field label -> value). Never stores CSRF/hidden tokens.",
    )
    session_state_json = models.JSONField(
        null=True,
        blank=True,
        help_text="Optional login cookies/storage for explicit injection into a fresh context (not form anti-fraud tokens).",
    )
    resume_file_path = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Path to the exported optimized resume (PDF/DOCX) used for upload.",
    )
    error_code = models.CharField(max_length=32, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when a worker begins processing a step; used to detect stuck attempts.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return f"ApplicationAttempt {self.id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.SUCCEEDED, self.Status.FAILED)

    def mark_failed(self, error_code: str, message: str = "", save: bool = True) -> None:
        self.status = self.Status.FAILED
        self.error_code = error_code or ""
        self.error_message = (message or "")[:4000]
        if save:
            self.save(update_fields=["status", "error_code", "error_message", "updated_at"])


class ApplicationAttemptStep(models.Model):
    """Audit log row for one step of an ApplicationAttempt (screenshots, actions, network)."""

    attempt = models.ForeignKey(
        ApplicationAttempt,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    step_name = models.CharField(max_length=64)
    message = models.TextField(blank=True, default="")
    screenshot_path = models.CharField(max_length=1024, blank=True, default="")
    action_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text="Structured snapshot of actions taken / fields filled in this step.",
    )
    network_log = models.JSONField(
        default=list,
        blank=True,
        help_text="XHR/fetch responses observed during this step (catches silent ATS validation errors).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.attempt_id}: {self.step_name}"


class AtsAutoSubmitStats(models.Model):
    """
    Per-user per-ATS counters that gate graduation from semi-auto to full-auto.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ats_auto_submit_stats",
    )
    ats_type = models.CharField(max_length=32)
    clean_submit_streak = models.PositiveIntegerField(
        default=0,
        help_text="Consecutive approved submissions with no human corrections. Resets to 0 on any correction.",
    )
    total_submits = models.PositiveIntegerField(default=0)
    total_corrections = models.PositiveIntegerField(default=0)
    full_auto_enabled = models.BooleanField(
        default=False,
        help_text="When true (and global mode allows), this ATS may submit without review.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        verbose_name = "ATS auto-submit stats"
        verbose_name_plural = "ATS auto-submit stats"
        unique_together = [("owner", "ats_type")]

    def __str__(self):
        return f"{self.ats_type}: streak={self.clean_submit_streak} full_auto={self.full_auto_enabled}"


class UserExperienceSettings(models.Model):
    """
    Per-user UI experience mode and onboarding progress.

    Normal mode: simplified nav and guided onboarding.
    Power mode: full builder surfaces (prompts, workflows, optimizer setup step).
    """

    class ExperienceMode(models.TextChoices):
        NORMAL = "normal", "Normal"
        POWER = "power", "Power"

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="experience_settings",
    )
    experience_mode = models.CharField(
        max_length=16,
        choices=ExperienceMode.choices,
        default=ExperienceMode.NORMAL,
    )
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    onboarding_dismissed_at = models.DateTimeField(null=True, blank=True)
    step_llm_connected = models.BooleanField(default=False)
    step_resume_uploaded = models.BooleanField(default=False)
    step_first_search = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the account email was verified. Null means unverified.",
    )
    pending_email = models.EmailField(
        blank=True,
        default="",
        help_text="Email awaiting verification after an address change.",
    )

    class Meta:
        verbose_name = "User experience settings"
        verbose_name_plural = "User experience settings"

    def __str__(self):
        return f"{self.owner_id} ({self.experience_mode})"

    @classmethod
    def get_for_user(cls, user):
        obj, _created = cls.objects.get_or_create(owner=user)
        return obj


class ImpersonationAuditLog(models.Model):
    """Audit trail for support staff login-as-user sessions."""

    hijacker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="impersonations_started",
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="impersonations_received",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        permissions = [
            ("can_impersonate_users", "Can impersonate users for support"),
        ]

    def __str__(self):
        return f"{self.hijacker_id} → {self.target_id} @ {self.started_at}"


class Plan(models.Model):
    """Commercial plan with daily quotas and feature flags. 0 = unlimited."""

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    llm_requests_per_day = models.PositiveIntegerField(
        default=50,
        help_text="Daily LLM invoke cap. 0 = unlimited.",
    )
    job_searches_per_day = models.PositiveIntegerField(
        default=20,
        help_text="Daily job-search runs. 0 = unlimited.",
    )
    apply_runs_per_day = models.PositiveIntegerField(
        default=5,
        help_text="Daily apply-agent attempt starts. 0 = unlimited.",
    )
    storage_mb = models.PositiveIntegerField(
        default=250,
        help_text="Soft storage budget in MB. 0 = unlimited (enforcement optional).",
    )
    api_access = models.BooleanField(default=False)
    stripe_price_id = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Plan.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


class Subscription(models.Model):
    """Per-user subscription binding to a Plan (Stripe-backed when configured)."""

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"
        UNPAID = "unpaid", "Unpaid"

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(
        Plan,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subscriptions",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    stripe_customer_id = models.CharField(max_length=128, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=128, blank=True, default="")
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"

    def __str__(self):
        plan = self.plan.slug if self.plan_id else "?"
        return f"{self.owner_id}:{plan}:{self.status}"


class UsageCounter(models.Model):
    """Durable per-day usage ledger for quota enforcement."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="usage_counters",
    )
    metric = models.CharField(max_length=64, db_index=True)
    period_date = models.DateField(db_index=True)
    count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OwnedManager()

    class Meta:
        unique_together = [("owner", "metric", "period_date")]
        indexes = [
            models.Index(fields=["owner", "period_date"]),
        ]

    def __str__(self):
        return f"{self.owner_id} {self.metric}@{self.period_date}={self.count}"


class CustomerApiKey(models.Model):
    """Hashed customer API key for programmatic access."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
    )
    name = models.CharField(max_length=128, blank=True, default="")
    prefix = models.CharField(max_length=16, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = OwnedManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.prefix}… ({self.owner_id})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class StripeWebhookEvent(models.Model):
    """Idempotency log for Stripe webhooks."""

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=128, blank=True, default="")
    processed_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField(null=True, blank=True)

    def __str__(self):
        return self.event_id
