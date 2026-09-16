from django.db import models


class AiClassificationRaw(models.Model):
    """One row per Groq call — the exact request and response, stored for
    debugging and audit. The `ai_classification_log` row points to this via
    `log_id`.
    
    This is append-only: every classify call writes a new raw row, even if the
    same job is classified again later.
    """

    request_payload = models.JSONField(
        help_text="The exact JSON sent to Groq (model, messages, temperature, etc.).",
    )
    response_payload = models.JSONField(
        help_text="The exact JSON returned by Groq (full chat completion).",
    )
    model = models.CharField(
        max_length=128,
        help_text="Groq model used for this call.",
    )
    status = models.CharField(
        max_length=32,
        help_text="Outcome: 'ok', 'error', or 'uncategorized'.",
    )
    latency_ms = models.IntegerField(
        help_text="Milliseconds from request start to parsed response.",
        default=0,
    )
    # ── Token accounting ─────────────────────────────────────────────────
    # tokens_sent / tokens_received / total_tokens come from the provider's
    # `usage` field in the response — the EXACT counts the model made. The
    # local estimate (est_tokens_sent) is a tiktoken cross-check computed
    # before the call. jobs_sent is the size of the whole classify batch.
    tokens_sent = models.IntegerField(
        null=True,
        blank=True,
        help_text="Exact prompt tokens sent (usage.prompt_tokens from Groq), or null if unavailable.",
    )
    tokens_received = models.IntegerField(
        null=True,
        blank=True,
        help_text="Exact completion tokens received (usage.completion_tokens from Groq), or null if unavailable.",
    )
    total_tokens = models.IntegerField(
        null=True,
        blank=True,
        help_text="Exact total tokens (usage.total_tokens from Groq), or null if unavailable.",
    )
    est_tokens_sent = models.IntegerField(
        null=True,
        blank=True,
        help_text="Local tiktoken estimate of the request payload, computed before the call.",
    )
    jobs_sent = models.IntegerField(
        null=True,
        blank=True,
        help_text="How many jobs were in the whole classify batch this call was part of.",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the Groq call happened.",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI classification raw call"
        verbose_name_plural = "AI classification raw calls"

    def __str__(self):
        return f"groq/{self.model} — {self.status} @ {self.created_at}"


class AiClassificationLog(models.Model):
    """One row per classify attempt on a job — the parsed result plus the
    recorded state before the call.

    Every classify call writes a new row, even for the same job, so there is a
    full history of every classification attempt. The latest row for a job is
    referenced from `Jobs.ClassificationId` on the .NET side.
    
    The raw Groq call for this attempt is stored in `AiClassificationRaw` and
    linked via `log_id`.
    """

    job_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text="The .NET Job.Id (UUID string) that was classified.",
    )
    sector_slug = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Canonical sector slug chosen by the LLM, or empty if uncategorized.",
    )
    sector_name = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text="Canonical sector name chosen by the LLM, or empty if uncategorized.",
    )
    confidence = models.FloatField(
        null=True,
        blank=True,
        help_text="LLM confidence in the chosen sector (0..1), or null.",
    )
    reasoning = models.TextField(
        blank=True,
        default="",
        help_text="LLM reasoning for the chosen sector, or empty.",
    )
    categorized = models.BooleanField(
        help_text="True when the LLM assigned a real sector; False when uncategorized.",
    )
    original_sector_slug = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Sector slug the job had before this classify call (sent by .NET).",
    )
    original_sector_name = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text="Sector name the job had before this classify call (sent by .NET).",
    )
    ai_classified_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this classify call happened (moved here from Jobs.AiClassifiedAt).",
    )

    # Link to the exact raw Groq call for this attempt.
    log_id = models.OneToOneField(
        "ai_service.AiClassificationRaw",
        on_delete=models.CASCADE,
        related_name="classification_log",
        help_text="The raw request/response row for this classify attempt.",
    )

    class Meta:
        ordering = ["-ai_classified_at"]
        verbose_name = "AI classification log"
        verbose_name_plural = "AI classification logs"

    def __str__(self):
        return f"job={self.job_id} slug={self.sector_slug or '(uncategorized)'} @ {self.ai_classified_at}"


class AIRequestLog(models.Model):
    """Logs AI API requests for debugging and cost tracking."""

    PROVIDER_CHOICES = [
        ("openai", "OpenAI"),
        ("groq", "Groq"),
        ("openrouter", "OpenRouter"),
        ("huggingface", "Hugging Face"),
    ]

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    model = models.CharField(max_length=100)
    feature = models.CharField(
        max_length=50,
        help_text="Which AI feature was used (e.g., 'resume_parsing', 'cover_letter').",
    )
    user_id = models.CharField(max_length=64, blank=True, default="")
    request_payload = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict)
    tokens_used = models.IntegerField(default=0)
    response_time_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.provider}/{self.model} — {self.feature} @ {self.created_at}"
