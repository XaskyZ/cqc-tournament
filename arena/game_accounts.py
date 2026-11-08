"""Telegram-linked, self-declared MLBB profiles. Lookup is not proof of ownership."""
import uuid
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from . import services
from .models import GameAccount, Participant, Team, TelegramIdentity, Tournament


def has_telegram(user):
    return user.is_authenticated and user.is_active and TelegramIdentity.objects.filter(user=user).exists()


class GameAccountForm(forms.Form):
    game_id = forms.RegexField(label='Game ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}))
    server_id = forms.RegexField(label='Server ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}))
    nickname = forms.CharField(label='Никнейм в игре', max_length=100, required=False, widget=forms.HiddenInput)
    accept_identity = forms.BooleanField(label='Я указываю свой игровой аккаунт. Организатор может запросить подтверждение в игре.', required=False)
    lookup_token = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .game_lookup import provider_enabled
        if provider_enabled():
            self.fields['nickname'].widget = forms.HiddenInput()

    def clean(self):
        data = super().clean()
        for name in ('game_id', 'server_id'):
            if name in data:
                if int(data[name]) <= 0:
                    self.add_error(name, 'ID должен быть положительным числом.')
                else:
                    data[name] = str(int(data[name]))
        return data


def provider_available():
    from .game_lookup import is_available
    return is_available()


def checked_profile(data):
    from .game_lookup import lookup, provider_enabled
    if provider_enabled():
        result = lookup(data['game_id'], data['server_id'])
        if (result.game_id, result.server_id) != (data['game_id'], data['server_id']):
            raise ValidationError('Сервис вернул другую пару идентификаторов. Профиль не сохранён.')
        return result.nickname, result.provider, timezone.now()
    raise ValidationError('Поиск игрового аккаунта пока недоступен. Ник должен быть получен по ID и серверу.')


def lookup_receipt(user, data, result):
    return signing.dumps({'user': user.pk, 'game_id': data['game_id'], 'server_id': data['server_id'],
                          'nickname': result[0], 'provider': result[1], 'checked_at': result[2].isoformat()},
                         salt='arena.game_lookup', compress=True)


def read_lookup_receipt(user, data):
    try:
        value = signing.loads(data.get('lookup_token', ''), salt='arena.game_lookup', max_age=600)
        checked_at = parse_datetime(value['checked_at'])
        if (value['user'] != user.pk or value['game_id'] != data['game_id']
                or value['server_id'] != data['server_id'] or value['provider'] != settings.MLBB_LOOKUP_PROVIDER
                or not isinstance(value['nickname'], str) or not value['nickname'].strip()
                or checked_at is None or timezone.is_naive(checked_at)):
            raise ValueError('Receipt mismatch')
        return value['nickname'], value['provider'], checked_at
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        raise ValidationError('Сначала нажмите «Найти аккаунт» и проверьте найденный ник. Результат действует 10 минут.') from None


def save_profile(user, data, *, lookup_snapshot=None):
    from .game_lookup import normalize_ids
    if not has_telegram(user):
        raise PermissionDenied('Сначала подключите Telegram.')
    if not data.get('accept_identity'):
        raise ValidationError('Подтвердите, что это ваш игровой аккаунт.')
    game_id, server_id = normalize_ids(data.get('game_id'), data.get('server_id'))
    data = {**data, 'game_id': game_id, 'server_id': server_id}
    # Provider HTTP deliberately happens before acquiring a write transaction.
    nickname, provider, checked_at = lookup_snapshot if lookup_snapshot is not None else checked_profile(data)
    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            current = GameAccount.objects.select_for_update().filter(user=user).first()
            if Participant.objects.filter(account=user, active=True).exists():
                raise ValidationError('Профиль уже связан с составом. Изменения согласуйте с организатором.')
            if GameAccount.objects.filter(game_id=data['game_id'], server_id=data['server_id']).exclude(user=user).exists():
                raise ValidationError('Эти идентификаторы уже заявлены в другом профиле. Обратитесь к организатору.')
            if current is None:
                current = GameAccount(user=user)
            current.game_id, current.server_id = data['game_id'], data['server_id']
            current.nickname, current.lookup_provider, current.lookup_checked_at = nickname, provider, checked_at
            current.full_clean()
            current.save()
            return current
    except IntegrityError as exc:
        raise ValidationError('Этот игровой аккаунт уже связан с другим профилем.') from exc


@login_required
def game_account(request):
    from .game_lookup import LookupUnavailable, LookupNotFound, provider_enabled
    account = GameAccount.objects.filter(user=request.user).first()
    linked = has_telegram(request.user)
    return_token = request.GET.get('participant_token') or request.POST.get('participant_token')
    return_team = request.GET.get('team_token') or request.POST.get('team_token')
    if return_team:
        try:
            normalized_team = uuid.UUID(return_team)
            if Team.objects.filter(invite_token=normalized_team).exists():
                request.session['team_return_token'] = str(normalized_team)
                request.session.pop('participant_return_token', None)
        except (ValueError, TypeError):
            pass
    if return_token:
        try:
            normalized_token = uuid.UUID(return_token)
            if Participant.objects.filter(token=normalized_token).exists():
                request.session['participant_return_token'] = str(normalized_token)
                request.session.pop('team_return_token', None)
        except (ValueError, TypeError):
            pass
    locked = Participant.objects.filter(account=request.user, active=True).exists()
    initial = {name: getattr(account, name) for name in ('game_id', 'server_id', 'nickname')} if account else {}
    form = GameAccountForm(request.POST if request.method == 'POST' else None, initial=initial)
    result = None
    saved = False
    action = request.POST.get('action', '')
    if request.method == 'POST':
        if not linked:
            raise PermissionDenied('Сначала подключите Telegram.')
        if form.is_valid():
            try:
                if locked:
                    raise ValidationError('Профиль связан с составом. Изменения согласуйте с организатором.')
                if action == 'lookup':
                    if not provider_available():
                        raise ValidationError('Автоматическая проверка ника пока не подключена.')
                    result = checked_profile(form.cleaned_data)
                    # Show the authoritative value and ask the user to review before save.
                    data = form.data.copy()
                    data['nickname'] = result[0]
                    data['lookup_token'] = lookup_receipt(request.user, form.cleaned_data, result)
                    data['accept_identity'] = ''
                    form = GameAccountForm(data)
                elif action == 'save':
                    result = read_lookup_receipt(request.user, form.cleaned_data)
                    account = save_profile(request.user, form.cleaned_data, lookup_snapshot=result)
                    saved = True
                else:
                    raise ValidationError('Выберите проверку или сохранение профиля.')
            except (ValidationError, LookupUnavailable, LookupNotFound) as exc:
                form.add_error(None, exc if isinstance(exc, ValidationError) else str(exc))
    valid_return_token = None
    try:
        candidate = uuid.UUID(str(request.session.get('participant_return_token')))
        if Participant.objects.filter(token=candidate).exists():
            valid_return_token = candidate
    except (ValueError, TypeError):
        pass
    from .telegram_auth import _valid_team_token
    valid_team_token = _valid_team_token(request.session.get('team_return_token'))
    context = {'form': form, 'game_account': account, 'participant_token': valid_return_token,
               'team_invite_token': valid_team_token,
               'profile_locked': locked, 'telegram_linked': linked, 'nickname_lookup_available': provider_available(),
               'nickname_lookup_enabled': True, 'lookup_result': result, 'profile_saved': saved}
    if request.method == 'POST' and 'application/json' in request.headers.get('Accept', ''):
        ok = not bool(form.errors)
        return JsonResponse({'ok': ok, 'action': action, 'saved': saved,
                             'html': render_to_string('arena/partials/game_account_editor.html', context, request=request)},
                            status=200 if ok else 400)
    return render(request, 'arena/game_account.html', context)


@transaction.atomic
def claim_participant(user, token):
    if not has_telegram(user):
        raise PermissionDenied('Для подтверждения участия подключите Telegram.')
    existing = get_object_or_404(Participant, token=token)
    Tournament.objects.select_for_update().get(pk=existing.tournament_id)
    team = Team.objects.select_for_update().get(pk=existing.team_id)
    get_user_model().objects.select_for_update().get(pk=user.pk)
    profile = GameAccount.objects.select_for_update().filter(user=user).first()
    player = Participant.objects.select_for_update().get(pk=existing.pk)
    player.team = team
    if not player.active:
        raise ValidationError('Это место выведено из состава. Вступите через новое приглашение.')
    if player.account_id and player.account_id != user.pk:
        raise PermissionDenied('Участник уже связан с другим аккаунтом.')
    if not profile:
        raise ValidationError('Сначала заполните игровой профиль.')
    if (player.game_id, player.server_id) != (profile.game_id, profile.server_id):
        raise ValidationError('Game ID и Server ID не совпадают с составом команды. Обратитесь к капитану.')
    if Team.objects.filter(tournament=player.tournament, captain=user).exclude(pk=player.team_id).exists():
        raise ValidationError('Вы уже капитан другой команды этого турнира.')
    if Participant.objects.filter(tournament=player.tournament, account=user, active=True).exclude(pk=player.pk).exists():
        raise ValidationError('Этот Telegram уже связан с другим участником турнира.')
    if player.team.status == 'rejected':
        raise ValidationError('Команда отклонена организатором.')
    if player.team.status == 'confirmed' and not player.account_id:
        raise ValidationError('Команда уже допущена. Первичную привязку согласуйте с организатором.')
    if player.nickname != profile.nickname:
        player.nickname = profile.nickname
        # A changed roster must be seen again by the organizer before admission.
        if player.team.roster_checked_at:
            player.team.roster_checked_at = None
            player.team.roster_checked_by = None
            player.team.save(update_fields=['roster_checked_at', 'roster_checked_by'])
    player.account = user
    player.save(update_fields=['account', 'nickname'])
    return services.confirm_registration(player)


@require_POST
@login_required
def participant_claim(request, token):
    try:
        if request.POST.get('accept_identity') != 'on':
            raise ValidationError('Подтвердите, что игровой аккаунт в составе принадлежит вам.')
        claim_participant(request.user, token)
        messages.success(request, 'Telegram и игровой профиль связаны с заявкой. Участие подтверждено.')
    except ValidationError as exc:
        for message in exc.messages:
            messages.error(request, message)
    return redirect('participant', token=token)
