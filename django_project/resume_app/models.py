from django.db import models
from django.utils import timezone


class Track(models.Model):
    """
    Preference / search track, e.g. IC vs Management.

    slug is the stable identifier used in URL/query params and on related
    models (JobSearchTask.track, PipelineEntry.track, etc.).
    """

    slug = models.SlugField(
        max_length=32,
        unique=True,
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

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.label or self.slug

    @classmethod
    def ensure_baseline(cls):
        """
        Ensure there is at least one track so old installs keep working.

        If no Track rows exist yet, seed the original two defaults (ic, mgmt).
        On subsequent calls it returns existing tracks without recreating any
        that were intentionally deleted by the user.
        """
        if not cls.objects.exists():
            baseline = [
                ("ic", "IC (Principal / Staff)"),
                ("mgmt", "Management (Manager / Director)"),
            ]
            for slug, label in baseline:
                cls.objects.get_or_create(slug=slug, defaults={"label": label})
            if not cls.objects.filter(is_default=True).exists():
                cls.objects.filter(slug="ic").update(is_default=True)
        return cls.objects.all()

    @classmethod
    def get_default_slug(cls) -> str:
        """
        Best-effort default track slug. If no tracks exist, create a simple
        default 'ic' track so callers always have something usable.
        """
        default = cls.objects.filter(is_default=True).first()
        if default:
            return default.slug
        first = cls.objects.order_by("id").first()
        if first:
            return first.slug
        # No tracks yet: create a minimal default IC track.
        obj, _created = cls.objects.get_or_create(
            slug="ic",
            defaults={"label": "IC (Principal / Staff)", "is_default": True},
        )
        return obj.slug


class UserResume(models.Model):
    file = models.FileField(upload_to='resumes/')
    original_filename = models.CharField(max_length=255, blank=True, help_text="Original name of the uploaded file")
    track = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Preferred preference track slug for this resume (used to default Job Search track).",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_filename or f"Resume {self.id} uploaded at {self.uploaded_at}"

class JobDescription(models.Model):
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class OptimizedResume(models.Model):
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

class AgentLog(models.Model):
    optimized_resume = models.ForeignKey(OptimizedResume, related_name='logs', on_delete=models.CASCADE)
    step_name = models.CharField(max_length=100)
    thought = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class UserPromptProfile(models.Model):
    """
    Singleton-style profile (use id=1) for edited Writer / Judge / Matching / Insights prompts.
    Empty string for a field means fall back to the code default in prompts.py.
    """

    writer = models.TextField(blank=True)
    ats_judge = models.TextField(blank=True)
    recruiter_judge = models.TextField(blank=True)
    matching = models.TextField(blank=True)
    insights = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User prompt profile"
        verbose_name_plural = "User prompt profiles"

    def __str__(self):
        return "Prompt profile"


class LLMProviderConfig(models.Model):
    """Stored API key and default model per provider. Key is encrypted at rest."""
    provider = models.CharField(max_length=64, unique=True)
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

    class Meta:
        verbose_name = "LLM Provider Config"
        verbose_name_plural = "LLM Provider Configs"

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
    priority = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.provider_config.provider} / {self.model or '(default)'} @ {self.priority}"


class AppAutomationSettings(models.Model):
    """
    Singleton (use pk=1): pipeline / vetting automation thresholds.
    """

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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "App automation settings"
        verbose_name_plural = "App automation settings"

    def __str__(self):
        return "App automation settings"

    @classmethod
    def get_solo(cls):
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "pipeline_to_vetting_enabled": False,
                "pipeline_preference_margin_min": 0,
                "vetting_to_applying_enabled": False,
                "vetting_interview_probability_min": 70,
            },
        )
        return obj


class JobListing(models.Model):
    """A job fetched from a source (e.g. JobSpy Indeed/Google). Deduplicated by (source, external_id)."""
    source = models.CharField(max_length=64)  # e.g. jobspy_indeed, jobspy_google
    external_id = models.CharField(max_length=256)
    title = models.CharField(max_length=512)
    company_name = models.CharField(max_length=512)
    location = models.CharField(max_length=512, blank=True)
    description = models.TextField(blank=True)
    url = models.URLField(max_length=2048, blank=True)
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
    """User actions on job listings: liked, disliked, saved."""

    class ActionType(models.TextChoices):
        LIKED = "liked", "Liked"
        DISLIKED = "disliked", "Disliked"
        SAVED = "saved", "Saved"

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    action = models.CharField(max_length=16, choices=ActionType.choices)
    track = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Preference track slug for likes/dislikes/saves (e.g. 'ic', 'mgmt'). Empty for legacy/global rows.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Allow the same job to be liked/saved in multiple tracks independently.
        # Legacy rows without track will keep working because track="".
        unique_together = [("job_listing", "action", "track")]


class JobListingEmbedding(models.Model):
    class EmbeddingType(models.TextChoices):
        LIKED = "liked", "Liked"
        DISLIKED = "disliked", "Disliked"

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    embedding_type = models.CharField(
        max_length=16,
        choices=EmbeddingType.choices,
        default=EmbeddingType.LIKED,
    )
    track = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text="Preference track: 'ic' or 'mgmt' for likes/dislikes; empty for legacy/global.",
    )
    embedding = models.JSONField(help_text="List of floats, e.g. 384-dim from sentence-transformers")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("job_listing", "embedding_type", "track")]


class JobListingTrackMetrics(models.Model):
    """
    Cached per-track preference metrics for pipeline / saved jobs.

    One row per (job_listing, track) so we can support arbitrary tracks
    beyond the original IC / Management split.
    """

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    track = models.CharField(
        max_length=32,
        help_text="Track slug, e.g. 'ic', 'mgmt', or a custom track.",
    )
    focus_percent = models.IntegerField(null=True, blank=True)
    focus_after_penalty = models.IntegerField(null=True, blank=True)
    preference_margin = models.IntegerField(null=True, blank=True)
    last_scored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("job_listing", "track")]
        indexes = [
            models.Index(fields=["track", "job_listing"]),
        ]

def _normalize_disqualifier_phrase(phrase: str) -> str:
    """Lowercase, strip, collapse whitespace for uniqueness."""
    if not phrase or not isinstance(phrase, str):
        return ""
    return " ".join((phrase or "").lower().strip().split())


class UserDisqualifier(models.Model):
    """
    Words or phrases the user wants to avoid in job descriptions.
    Chosen from disliked jobs; any job whose description contains one of these
    is excluded first (before other filters).
    """
    phrase = models.CharField(max_length=500, unique=True)  # stored normalized (lower, collapsed space)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["phrase"]

    def save(self, *args, **kwargs):
        self.phrase = _normalize_disqualifier_phrase(self.phrase)
        super().save(*args, **kwargs)


class OptimizerWorkflow(models.Model):
    """Saved custom workflow for Resume Optimization: step order, loop target, exit condition."""
    name = models.CharField(max_length=255)
    steps = models.JSONField(
        help_text="Ordered list of step ids, e.g. ['writer', 'ats_judge', 'recruiter_judge']"
    )
    loop_to = models.CharField(max_length=64, blank=True)
    max_iterations = models.PositiveSmallIntegerField(default=3)
    score_threshold = models.PositiveSmallIntegerField(default=85)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class JobMatchResult(models.Model):
    """Fit-check result for a (job_listing, resume) pair. One per job per resume."""
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

    class Meta:
        unique_together = [("job_listing", "resume")]
        ordering = ["-analyzed_at"]

    def __str__(self):
        return f"Match {self.job_listing_id} x resume {self.resume_id} ({self.fit_score})"


class JobSearchTask(models.Model):
    """Scheduled job search: runs on cron, accumulates results into pipeline by track."""
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

    class Stage(models.TextChoices):
        PIPELINE = "pipeline", "Pipeline"
        VETTING = "vetting", "Vetting"
        APPLYING = "applying", "Applying"
        DONE = "done", "Done"
        DELETED = "deleted", "Deleted"

    job_listing = models.ForeignKey(JobListing, on_delete=models.CASCADE)
    track = models.CharField(
        max_length=32,
        help_text="Track slug for this pipeline row.",
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

    class Meta:
        unique_together = [("job_listing", "track")]
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
