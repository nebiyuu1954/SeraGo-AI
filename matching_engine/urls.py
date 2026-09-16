from django.urls import path

from . import views, docs_views

app_name = "matching_engine"

urlpatterns = [
    # API documentation
    path("docs/", docs_views.swagger_ui, name="swagger-ui"),
    path("redoc/", docs_views.redoc_ui, name="redoc"),
    path("schema/", docs_views.schema_json_view, name="schema-json"),

    # Feature 1 — webhook called by .NET when a job is published
    path("webhook/job-published", views.webhook_job_published, name="webhook-job-published"),

    # Feature 2 — webhook called by .NET when a talent applies
    path("webhook/application", views.webhook_application, name="webhook-application"),

    # Talent-initiated — the For You page's "Run AI matching" button
    path("refresh-for-you", views.refresh_for_you, name="refresh-for-you"),

    # Recruiter-initiated — the applications page's "Run AI matching" button
    path("applications/rescore", views.applications_rescore, name="applications-rescore"),

    # Batch reads (annotate .NET pages with stored scores)
    path("scores/batch", views.scores_batch, name="scores-batch"),
    path("application-scores/batch", views.application_scores_batch, name="application-scores-batch"),

    # Resume → profile fields (called by .NET with a presigned R2 URL)
    path("parse-resume", views.parse_resume, name="parse-resume"),

    # Health check
    path("health", views.health, name="health"),
]
