from django.contrib import admin

from .models import JobVector, TalentJobScore, TalentProfileVector


@admin.register(TalentProfileVector)
class TalentProfileVectorAdmin(admin.ModelAdmin):
    list_display = ("talent_user_id", "profile_hash", "created_at", "updated_at")
    search_fields = ("talent_user_id",)
    readonly_fields = ("embedding", "keywords", "created_at", "updated_at")


@admin.register(JobVector)
class JobVectorAdmin(admin.ModelAdmin):
    list_display = ("job_id", "created_at")
    search_fields = ("job_id",)
    readonly_fields = ("embedding", "keywords", "created_at")


@admin.register(TalentJobScore)
class TalentJobScoreAdmin(admin.ModelAdmin):
    list_display = ("talent_user_id", "job_id", "score", "created_at", "updated_at")
    list_filter = ("score",)
    search_fields = ("talent_user_id", "job_id")
    readonly_fields = ("matched", "missing", "explanation")
