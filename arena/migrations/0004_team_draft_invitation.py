import uuid

from django.db import migrations, models


def populate_invites(apps, schema_editor):
    Team = apps.get_model("arena", "Team")
    for team in Team.objects.using(schema_editor.connection.alias).all().iterator():
        team.invite_token = uuid.uuid4()
        team.save(using=schema_editor.connection.alias, update_fields=["invite_token"])


class Migration(migrations.Migration):
    dependencies = [("arena", "0003_telegram_auth_game_accounts")]

    operations = [
        migrations.AddField(model_name="team", name="invite_token", field=models.UUIDField(null=True, editable=False)),
        migrations.RunPython(populate_invites, migrations.RunPython.noop),
        migrations.AlterField(model_name="team", name="invite_token", field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
        migrations.AddField(model_name="team", name="submitted_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name="team", name="status", field=models.CharField(choices=[("draft", "Сбор состава"), ("pending", "Ожидает проверки"), ("confirmed", "Допущена"), ("rejected", "Отклонена")], default="pending", max_length=16)),
    ]
