from django.db import migrations, models


def label_completed_games(apps, schema_editor):
    Match = apps.get_model("arena", "Match")
    Match.objects.using(schema_editor.connection.alias).filter(
        completed_at__isnull=False, winner__isnull=False,
    ).update(resolution="played")


class Migration(migrations.Migration):
    dependencies = [("arena", "0004_team_draft_invitation")]
    operations = [
        migrations.AddField(model_name="match", name="resolution", field=models.CharField(
            blank=True, choices=[("played", "Сыгран"), ("bye", "Проход без игры"), ("empty", "Пустой узел сетки")], default="", max_length=8,
        )),
        migrations.RunPython(label_completed_games, migrations.RunPython.noop),
    ]
