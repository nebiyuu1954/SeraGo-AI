"""
AI service views — placeholder for future LLM-powered features:
  - Resume parsing
  - Cover letter generation
  - Job description enhancement
  - Candidate scoring with explanations
"""

from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    """Health check endpoint for the AI service."""
    return JsonResponse({
        "status": "ok",
        "service": "ai_service",
        "note": "AI features not yet implemented. Coming soon: resume parsing, cover letter generation, job enhancement.",
    })
