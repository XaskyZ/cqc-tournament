"""Explicit, versioned acceptance; no acceptance inferred from viewing a page."""
import hashlib
import json
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from .models import GameAccount, Participant, Team, TelegramIdentity, Tournament
from .rule_models import RulesAcceptance


def rules_snapshot(tournament):
    fields = ('title', 'game', 'rules', 'event_date', 'starts_at', 'end_registration',
              'timezone_name', 'max_teams', 'roster_size', 'reserve_limit', 'reserves_pay',
              'best_of', 'final_best_of', 'grand_final_reset', 'prize_pool', 'prize_note', 'terms_confirmed')
    result = {name: getattr(tournament, name) for name in fields}
    for key, value in result.items():
        if hasattr(value, 'isoformat'):
            result[key] = value.isoformat()
    result['participant_fee_kzt'] = 2200
    return result


def rules_version(tournament):
    return hashlib.sha256(json.dumps(rules_snapshot(tournament), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def has_accepted(tournament, user):
    return bool(user and user.is_authenticated and RulesAcceptance.objects.filter(
        tournament=tournament, user=user, version=rules_version(tournament)).exists())


@transaction.atomic
def accept_rules(tournament, user, version, consent):
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if consent is not True:
        raise ValidationError('Подтвердите ознакомление с условиями.')
    if not tournament.rules.strip():
        raise ValidationError('Организатор ещё не опубликовал правила.')
    if version != rules_version(tournament):
        raise ValidationError('Условия изменились. Прочитайте текущую версию и подтвердите её заново.')
    return RulesAcceptance.objects.get_or_create(
        tournament=tournament, user=user, version=version,
        defaults={'snapshot': rules_snapshot(tournament)})[0]


class AcceptanceForm(forms.Form):
    version = forms.CharField(widget=forms.HiddenInput, max_length=64)
    consent = forms.BooleanField(label='Я прочитал и принимаю показанную версию правил и условий турнира.')


@never_cache
def rules_page(request):
    tournament = get_object_or_404(Tournament.objects.order_by('pk'))
    version = rules_version(tournament)
    target = request.GET.get('next', request.POST.get('next', '/captain/'))
    if not target.startswith('/') or not url_has_allowed_host_and_scheme(target, allowed_hosts=set()):
        target = '/captain/'
    form = AcceptanceForm(request.POST if request.method == 'POST' else None, initial={'version': version})
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={reverse('rules')}")
        if form.is_valid():
            try:
                accept_rules(tournament, request.user, form.cleaned_data['version'], form.cleaned_data['consent'])
                messages.success(request, 'Версия правил и время вашего согласия сохранены.')
                return redirect(target)
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(request, 'arena/rules.html', {
        'tournament': tournament, 'rules_version': version, 'form': form, 'return_to': target,
        'rules_accepted': has_accepted(tournament, request.user),
    })


class RulesAcceptanceMiddleware:
    """Guard user-facing commitment actions; never replay the original POST."""
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method != 'POST' or not request.user.is_authenticated:
            return None
        action = request.resolver_match.url_name
        if action not in {'team_submit', 'payment_submit', 'team_join', 'participant_confirm', 'participant_claim'}:
            return None
        if not request.user.is_active or not TelegramIdentity.objects.filter(user=request.user).exists():
            return None
        if action in {'team_join', 'participant_confirm', 'participant_claim'} and not GameAccount.objects.filter(user=request.user).exists():
            return None
        if action == 'team_submit':
            subject = Team.objects.select_related('tournament').filter(pk=view_kwargs.get('team_id'), captain=request.user).first()
            target = '/captain/'
        elif action == 'team_join':
            subject = Team.objects.select_related('tournament').filter(invite_token=view_kwargs.get('invite_token')).first()
            target = request.path
        else:
            subject = Participant.objects.select_related('tournament').filter(token=view_kwargs.get('token'), active=True).first()
            if subject and action == 'payment_submit' and subject.account_id != request.user.pk:
                return None
            target = reverse('participant', args=[view_kwargs['token']])
        if subject is None:
            return None  # Let the target view preserve its 403/404 contract.
        with transaction.atomic():
            tournament = Tournament.objects.select_for_update().get(pk=subject.tournament_id)
            if not has_accepted(tournament, request.user):
                messages.info(request, 'Перед вступлением, подачей заявки или взноса подтвердите текущие условия турнира.')
                return redirect(reverse('rules') + '?' + urlencode({'next': target}))
            # The condition and actual mutation use the same tournament lock.
            # CSRF has already run; this middleware is last in the view chain.
            return view_func(request, *view_args, **view_kwargs)

    def __call__(self, request):
        return self.get_response(request)
