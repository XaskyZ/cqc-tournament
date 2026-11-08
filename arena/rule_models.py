from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class AcceptanceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Историю согласия с правилами нельзя изменять.')

    def delete(self):
        raise ValidationError('Историю согласия с правилами нельзя удалять.')


class RulesAcceptance(models.Model):
    objects = AcceptanceQuerySet.as_manager()
    tournament = models.ForeignKey('arena.Tournament', on_delete=models.PROTECT, related_name='rules_acceptances')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    version = models.CharField(max_length=64)
    snapshot = models.JSONField()
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tournament', 'user', 'version'], name='one_rules_acceptance_version')]
        ordering = ['-accepted_at']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Историю согласия с правилами нельзя изменять.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Историю согласия с правилами нельзя удалять.')
