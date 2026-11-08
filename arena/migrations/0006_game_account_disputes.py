import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("arena", "0005_match_resolution"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="GameAccountDispute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("game_id", models.CharField(max_length=40)),
                ("server_id", models.CharField(max_length=40)),
                ("nickname", models.CharField(max_length=100)),
                ("lookup_provider", models.CharField(max_length=80)),
                ("lookup_checked_at", models.DateTimeField()),
                ("explanation", models.TextField(max_length=2000)),
                ("old_account_pk", models.PositiveBigIntegerField()),
                ("old_account_updated_at", models.DateTimeField()),
                ("claimant_account_pk", models.PositiveBigIntegerField(blank=True, null=True)),
                ("claimant_account_updated_at", models.DateTimeField(blank=True, null=True)),
                ("claimant_previous_game_id", models.CharField(blank=True, max_length=40)),
                ("claimant_previous_server_id", models.CharField(blank=True, max_length=40)),
                ("claimant_previous_nickname", models.CharField(blank=True, max_length=100)),
                ("status", models.CharField(choices=[("pending", "На рассмотрении"), ("approved", "Привязка исправлена"), ("rejected", "Отклонено")], default="pending", max_length=10)),
                ("decision_reason", models.TextField(blank=True, max_length=1000)),
                ("checked_in_game", models.BooleanField(default=False)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claimant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="game_account_disputes", to=settings.AUTH_USER_MODEL)),
                ("old_owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="disputed_game_accounts", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_game_account_disputes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-pk"], "constraints": [models.UniqueConstraint(condition=models.Q(status="pending"), fields=("claimant", "game_id", "server_id"), name="one_pending_game_dispute_per_claimant")]},
        ),
    ]
