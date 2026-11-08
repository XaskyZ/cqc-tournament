from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('arena', '0006_game_account_disputes')]
    operations = [
        migrations.AddField(model_name='participant', name='active', field=models.BooleanField(default=True)),
        migrations.AddField(model_name='participant', name='withdrawn_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='participant', name='withdrawal_reason', field=models.TextField(blank=True)),
        migrations.AddField(model_name='participant', name='withdrawn_by', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='withdrawn_players', to=settings.AUTH_USER_MODEL)),
        migrations.RemoveConstraint(model_name='participant', name='unique_player_per_tournament'),
        migrations.AddConstraint(model_name='participant', constraint=models.UniqueConstraint(fields=('tournament', 'game_id', 'server_id'), condition=models.Q(active=True), name='unique_active_player_per_tournament')),
    ]
