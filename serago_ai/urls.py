from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/matching/", include("matching_engine.urls")),
    path("api/ai/", include("ai_service.urls")),
]
