"""Create a team, share an invitation, then submit the assembled roster."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import services, team_services
from .forms import JoinTeamForm, ManualPlayerForm, TeamRegistrationForm
from .game_accounts import has_telegram
from .game_lookup import LookupNotFound, LookupUnavailable
from .models import GameAccount, Participant, Payment, Team, TelegramIdentity


def user_team(user, tournament):
    if not user.is_authenticated:
        return None
    own = Team.objects.filter(captain=user, tournament=tournament).select_related('captain', 'tournament').first()
    if own:
        return own
    membership = Participant.objects.filter(account=user, tournament=tournament, active=True).select_related('team__captain', 'team__tournament').first()
    return membership.team if membership else None


@login_required
def create_team_view(request):
    from .views import get_tournament
    tournament = get_tournament()
    existing = user_team(request.user, tournament)
    if existing:
        return redirect('captain')
    if not has_telegram(request.user):
        return render(request, 'arena/register.html', {'registration_requires_telegram': True})
    identity = TelegramIdentity.objects.get(user=request.user)
    form = TeamRegistrationForm(request.POST if request.method == 'POST' else None,
                                initial={'telegram_contact': '@' + identity.username if identity.username else ''})
    if request.method == 'POST' and form.is_valid():
        try:
            team_services.create_team(tournament, request.user, **form.cleaned_data)
            messages.success(request, 'Команда создана. Отправьте общую ссылку игрокам: каждый заполнит свой профиль и вступит сам.')
            return redirect('captain')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'arena/register.html', {'form': form, 'registration_errors': services.registration_errors(tournament)})


@login_required
def dashboard_view(request):
    from .views import get_tournament, state_version
    tournament = get_tournament()
    team = user_team(request.user, tournament)
    players = list(team.participants.filter(active=True).select_related('payment', 'account').order_by('pk')) if team else []
    own = next((p for p in players if p.account_id == request.user.pk), None)
    is_captain = bool(team and team.captain_id == request.user.pk)
    join_url = reverse('team_join', args=[team.invite_token]) if team else None
    errors = team_services.submission_errors(team) if team and team.status == 'draft' else []
    errors += services.registration_errors(tournament) if team and team.status == 'draft' else []
    # Members never receive other players' bearer links or game IDs in HTML.
    public_roster = [{'nickname': p.nickname, 'is_reserve': p.is_reserve} for p in players]
    editable = False
    if is_captain:
        try:
            team_services._revision_open(team)
            editable = True
        except ValidationError:
            pass
    contacts = dict(line.split(': ', 1) for line in team.contact.splitlines() if ': ' in line) if team else {}
    return render(request, 'arena/captain.html', {
        'team': team, 'participants': players if is_captain else public_roster, 'own_participant': own,
        'archived_participants': list(team.participants.filter(active=False).select_related('payment')) if is_captain else [],
        'archived_own': Participant.objects.filter(active=False, account=request.user).select_related('team', 'payment'),
        'is_captain': is_captain, 'join_url': join_url, 'invite_url': request.build_absolute_uri(join_url) if join_url else None,
        'submission_errors': errors, 'can_submit': bool(team and team.status == 'draft' and not errors),
        'manual_player_form': ManualPlayerForm(), 'readiness': services.team_readiness(team) if team else [],
        'live_status_url': reverse('captain_state'), 'live_status_version': state_version(team, players),
        'can_revise': editable, 'team_details_form': TeamDetailsForm(initial={'name': team.name if team else '', 'telegram_contact': contacts.get('Telegram', ''), 'discord_contact': contacts.get('Discord', '')}),
    })


def team_join(request, invite_token):
    team = get_object_or_404(Team.objects.select_related('tournament', 'captain'), invite_token=invite_token)
    # Invite context survives Telegram authentication and profile lookup/save.
    request.session['team_return_token'] = str(invite_token)
    request.session.pop('participant_return_token', None)
    profile = GameAccount.objects.filter(user=request.user).first() if request.user.is_authenticated else None
    own = team.participants.filter(active=True, account=request.user).select_related('payment').first() if request.user.is_authenticated else None
    players = list(team.participants.filter(active=True).order_by('pk'))
    main_count = sum(not p.is_reserve for p in players)
    reserved = next((p for p in players if profile and (p.game_id, p.server_id) == (profile.game_id, profile.server_id)), None)
    initial_role = 'reserve' if (reserved and reserved.is_reserve) or main_count >= team.tournament.roster_size else 'main'
    form = JoinTeamForm(request.POST if request.method == 'POST' else None, initial={'role': initial_role})
    errors = []
    if team.status not in ('draft', 'pending') or team.tournament.matches.exists():
        errors.append('Состав этой команды уже закрыт для вступления.')
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect('login')
        if form.is_valid():
            try:
                team_services.join_team(team, request.user, **form.cleaned_data)
                request.session.pop('team_return_token', None)
                messages.success(request, 'Вы вступили в команду. В кабинете доступны ваш состав и личная страница взноса.')
                return redirect('captain')
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(request, 'arena/team_join.html', {
        'team': team, 'tournament': team.tournament, 'team_invite_token': team.invite_token,
        'participants': [{'nickname': p.nickname, 'is_reserve': p.is_reserve} for p in players],
        'main_count': main_count, 'reserve_count': sum(p.is_reserve for p in players),
        'form': form, 'telegram_linked': has_telegram(request.user), 'game_account': profile,
        'own_participant': own, 'join_errors': errors,
    })


@require_POST
@login_required
def team_add_player(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    if team.captain_id != request.user.pk:
        raise PermissionDenied
    form = ManualPlayerForm(request.POST)
    if form.is_valid():
        try:
            team_services.add_player(team, request.user, **form.cleaned_data)
            messages.success(request, 'Аккаунт найден и добавлен в состав. Игрок должен войти по общей ссылке и подтвердить своё место.')
        except (ValidationError, LookupUnavailable, LookupNotFound) as exc:
            messages.error(request, ' '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    else:
        for errors in form.errors.values():
            messages.error(request, ' '.join(errors))
    return redirect('captain')


@require_POST
@login_required
def team_submit(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        team_services.submit_team(team, request.user)
        messages.success(request, 'Заявка команды подана на турнир. Состав закрыт; допуск будет после проверки участников и взносов.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('captain')


@require_POST
@login_required
def team_remove_player(request, team_id, participant_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        team_services.remove_player(team, request.user, participant_id)
        messages.success(request, 'Место освобождено. История оплаченных взносов не изменяется.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('captain')


class TeamDetailsForm(TeamRegistrationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop('accept_rules')


def _revision_response(request, team_id, operation, **kwargs):
    team = get_object_or_404(Team, pk=team_id)
    try:
        operation(team, request.user, **kwargs)
        messages.success(request, 'Изменения сохранены. История взносов сохранена.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('captain')


@require_POST
@login_required
def team_return_to_draft(request, team_id):
    return _revision_response(request, team_id, team_services.return_to_draft, reason=request.POST.get('reason', ''))


@require_POST
@login_required
def team_rotate_invite(request, team_id):
    return _revision_response(request, team_id, team_services.rotate_invite)


@require_POST
@login_required
def team_change_role(request, team_id, participant_id):
    return _revision_response(request, team_id, team_services.change_player_role,
                              participant_id=participant_id, role=request.POST.get('role', ''))


@require_POST
@login_required
def team_update_details(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    team_services._captain(team, request.user)
    form = TeamDetailsForm(request.POST)
    if form.is_valid():
        return _revision_response(request, team_id, team_services.update_team_details, **form.cleaned_data)
    for errors in form.errors.values():
        messages.error(request, ' '.join(errors))
    return redirect('captain')
