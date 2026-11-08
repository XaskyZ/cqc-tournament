"""Organizer-assisted replacement. Historical participants and payments stay intact."""
from django import forms
from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .models import Participant, Team, Tournament
from .services import _staff
from .views import staff_only


class WithdrawPlayerForm(forms.Form):
    reason = forms.CharField(label='Причина вывода из состава', max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))
    acknowledge = forms.BooleanField(label='Понимаю: старый платёж сохраняется, новому участнику назначается отдельный взнос. Возврат денег выполняется отдельно.')


@transaction.atomic
def withdraw_player(participant_id, actor, reason, acknowledge=False):
    _staff(actor)
    reason = str(reason or '').strip()
    if acknowledge is not True or not reason or len(reason) > 1000:
        raise ValidationError('Укажите причину и подтвердите сохранение финансовой истории.')
    existing = Participant.objects.get(pk=participant_id)
    tournament = Tournament.objects.select_for_update().get(pk=existing.tournament_id)
    team = Team.objects.select_for_update().get(pk=existing.team_id)
    player = Participant.objects.select_for_update().get(pk=participant_id)
    if not player.active:
        raise ValidationError('Участник уже выведен из состава.')
    if tournament.matches.exists() or any(value and timezone.now() >= value for value in (tournament.starts_at, tournament.end_registration)):
        raise ValidationError('Замена доступна до окончания регистрации и формирования сетки.')
    player.active = False
    player.withdrawn_at = timezone.now()
    player.withdrawn_by = actor
    player.withdrawal_reason = reason
    player.save(update_fields=['active', 'withdrawn_at', 'withdrawn_by', 'withdrawal_reason'])
    # Rejected teams remain rejected until a separate explicit review.
    if team.status != Team.Status.REJECTED:
        team.status = Team.Status.DRAFT
    team.submitted_at = None
    team.roster_checked_at = None
    team.roster_checked_by = None
    team.save(update_fields=['status', 'submitted_at', 'roster_checked_at', 'roster_checked_by'])
    LogEntry.objects.log_actions(user_id=actor.pk, queryset=Participant.objects.filter(pk=player.pk),
                                action_flag=CHANGE, change_message='Вывод из состава без изменения платежей: ' + reason)
    return player


@staff_only
def withdraw_player_view(request, participant_id):
    player = get_object_or_404(Participant.objects.select_related('team', 'payment'), pk=participant_id)
    form = WithdrawPlayerForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            withdraw_player(player.pk, request.user, **form.cleaned_data)
            messages.success(request, 'Место освобождено. История взноса сохранена; капитан должен заново подать состав.')
            return redirect('organizer')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'arena/withdraw_player.html', {'player': player, 'form': form})
