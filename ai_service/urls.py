from django.urls import path

from . import views

app_name = "ai_service"

urlpatterns = [
    path("health", views.health, name="health"),
]
