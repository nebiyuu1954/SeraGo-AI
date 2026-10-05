from django.urls import path

from . import views

app_name = "ai_service"

urlpatterns = [
    path("classify", views.classify, name="classify"),
    path("classify/log/<uuid:log_id>", views.get_log, name="get_log"),
]
