import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("arena", "0009_match_day"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [migrations.CreateModel(
        name="Notification",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("event_key", models.CharField(max_length=200)),
            ("title", models.CharField(max_length=180)),
            ("body", models.TextField(blank=True, max_length=2000)),
            ("path", models.CharField(default="/schedule/", max_length=250)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("read_at", models.DateTimeField(blank=True, null=True)),
            ("telegram_id", models.PositiveBigIntegerField(blank=True, null=True)),
            ("sent_at", models.DateTimeField(blank=True, null=True)),
            ("delivery_stopped_at", models.DateTimeField(blank=True, null=True)),
            ("attempts", models.PositiveIntegerField(default=0)),
            ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
            ("claim_token", models.UUIDField(blank=True, null=True)),
            ("claimed_until", models.DateTimeField(blank=True, null=True)),
            ("last_error", models.CharField(blank=True, max_length=150)),
            ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_notifications", to=settings.AUTH_USER_MODEL)),
        ],
        options={"ordering": ["-created_at", "-pk"],
                 "indexes": [models.Index(fields=["user", "read_at"], name="notification_user_read_idx")],
                 "constraints": [models.UniqueConstraint(fields=("user", "event_key"), name="unique_event_notification_user")]},
    )]
