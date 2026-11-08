"""Participant inbox. Financial/cashier notifications have a separate outbox."""

from django.conf import settings
from django.db import models


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_notifications")
    event_key = models.CharField(max_length=200)
    title = models.CharField(max_length=180)
    body = models.TextField(blank=True, max_length=2000)
    path = models.CharField(max_length=250, default="/schedule/")
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    # Capture the verified recipient at enqueue time; never redirect a private
    # event to a subsequently linked account or a user-supplied contact string.
    telegram_id = models.PositiveBigIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivery_stopped_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["user", "event_key"], name="unique_event_notification_user")]
        indexes = [models.Index(fields=["user", "read_at"], name="notification_user_read_idx")]
