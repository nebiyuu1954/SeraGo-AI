from django.db import models


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
