from django.db import models
from django.utils import timezone


class GitHubWebhookEvent(models.Model):
    """Model to store GitHub webhook data."""

    EVENT_TYPES = [
        ("pull_request", "Pull Request"),
        ("push", "Push"),
        ("issue_comment", "Issue Comment"),
    ]

    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    payload = models.JSONField()
    repository = models.CharField(max_length=255)
    sender = models.CharField(max_length=255)
    received_at = models.DateTimeField(default=timezone.now)
    processed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.event_type} from {self.repository} by {self.sender}"
