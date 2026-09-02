from django.urls import path

from . import views, docs_views

app_name = "matching_engine"

urlpatterns = [
    # API documentation
    path("docs/", docs_views.swagger_ui, name="swagger-ui"),
    path("redoc/", docs_views.redoc_ui, name="redoc"),
    path("schema/", docs_views.schema_json_view, name="schema-json"),

    # Webhook endpoints (called by .NET backend)
    path("webhook/job-published", views.webhook_job_published, name="webhook-job-published"),
    path("webhook/scraped-job", views.webhook_scraped_job, name="webhook-scraped-job"),
    path("webhook/application", views.webhook_application, name="webhook-application"),
    path("webhook/talent-updated", views.webhook_talent_updated, name="webhook-talent-updated"),
    path("webhook/talent-login", views.webhook_talent_login, name="webhook-talent-login"),

    # On-demand matching (talent)
    path("on-demand-match", views.on_demand_match, name="on-demand-match"),

    # On-demand matching (recruiter — application scoring)
    path("score-application", views.score_application, name="score-application"),

    # Read endpoints
    path("for-you/<str:user_id>", views.for_you, name="for-you"),
    path("talent-score/<str:user_id>/<str:job_id>", views.talent_job_score, name="talent-job-score"),
    path("application-score/<str:application_id>", views.application_score, name="application-score"),

    # Sector classification (called by scraper + .NET)
    path("classify-sector", views.classify_sector, name="classify-sector"),
    path("classify-sector/health", views.classify_sector_health, name="classify-sector-health"),

    # Health check
    path("health", views.health, name="health"),
]
