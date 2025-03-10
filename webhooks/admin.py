from django.contrib import admin
from .models import GitHubWebhookEvent


@admin.register(GitHubWebhookEvent)
class GitHubWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "repository", "sender", "received_at", "processed")
    list_filter = ("event_type", "repository", "processed")
    search_fields = ("repository", "sender")
    readonly_fields = ("payload", "received_at")
    ordering = ("-received_at",)
