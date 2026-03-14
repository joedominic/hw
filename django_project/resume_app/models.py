from django.db import models

class UserResume(models.Model):
    file = models.FileField(upload_to='resumes/')
    original_filename = models.CharField(max_length=255, blank=True, help_text="Original name of the uploaded file")
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

class AgentLog(models.Model):
    optimized_resume = models.ForeignKey(OptimizedResume, related_name='logs', on_delete=models.CASCADE)
    step_name = models.CharField(max_length=100)
    thought = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class LLMProviderConfig(models.Model):
    """Stored API key and default model per provider. Key is encrypted at rest."""
    provider = models.CharField(max_length=64, unique=True)
    encrypted_api_key = models.TextField(blank=True)
    default_model = models.CharField(max_length=128, blank=True)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "LLM Provider Config"
        verbose_name_plural = "LLM Provider Configs"


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
        max_length=16,
        blank=True,
        default="",
        help_text="Preference track: 'ic' or 'mgmt' for likes/dislikes; empty for legacy/global.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("job_listing", "action")]


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
