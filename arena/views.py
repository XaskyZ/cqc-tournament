from functools import wraps
import hashlib
import json
from itertools import groupby
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum, Prefetch
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from . import services
from .forms import MatchEditForm, PaymentReviewForm, SignupForm, TeamRegistrationForm, player_formset
from .models import Match, Participant, Payment, PaymentAudit, Team, Tournament
from .providers import ManualKaspiProvider


def get_tournament():
    tournament = Tournament.objects.order_by('pk').first()
    if not tournament:
        raise Http404('Турнир ещё не настроен.')
    return tournament


def staff_only(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_staff:
            raise PermissionDenied('Раздел доступен только организатору.')
        return view(request, *args, **kwargs)
    return wrapped


def domain_error(request, exc):
    for message in exc.messages:
        messages.error(request, message)


def match_queryset():
    return Match.objects.filter(tournament=get_tournament()).select_related('team_a', 'team_b', 'winner', 'source_a', 'source_b')


def home(request):
    tournament = get_tournament()
    return render(request, 'arena/home.html', {'confirmed_count': tournament.teams.filter(status='confirmed').count(), 'total_teams': tournament.teams.exclude(status='rejected').count(), 'latest_matches': match_queryset().filter(resolution__in=['played', 'forfeit']).order_by('-completed_at')[:3]})


def teams(request):
    tournament = get_tournament()
    return render(request, 'arena/teams.html', {'teams': tournament.teams.filter(status='confirmed').select_related('captain').prefetch_related(Prefetch('participants', queryset=Participant.objects.filter(active=True))).order_by('created_at'), 'pending_count': tournament.teams.filter(status='pending').count()})


def schedule(request):
    matches = match_queryset().filter(active=True).exclude(resolution__in=['bye', 'empty'])
    selected_team = None
    team_param = request.GET.get('team', '')
    if team_param.isascii() and team_param.isdigit() and len(team_param) <= 18:
        selected_team = Team.objects.filter(pk=team_param, tournament=get_tournament(), status='confirmed').first()
        if selected_team:
            matches = matches.filter(Q(team_a=selected_team) | Q(team_b=selected_team))
    return render(request, 'arena/schedule.html', {'matches': matches.order_by('scheduled_at', 'pk'), 'selected_team': selected_team})


def bracket(request):
    matches = list(match_queryset())
    order = {'upper': 0, 'lower': 1, 'final': 2, 'reset': 3}
    matches.sort(key=lambda m: (order[m.bracket], m.round, m.index))
    rounds = []
    for (bracket_name, number), group in groupby(matches, key=lambda m: (m.bracket, m.round)):
        group = list(group)
        rounds.append({'label': f'{group[0].get_bracket_display()} · {number}', 'matches': group})
    return render(request, 'arena/bracket.html', {'rounds': rounds})


def signup(request):
    if request.user.is_authenticated:
        return redirect('captain')
    form = SignupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('register')
    return render(request, 'arena/signup.html', {'form': form})


@login_required
def register(request):
    from .team_views import create_team_view
    return create_team_view(request)


@login_required
def captain(request):
    from .team_views import dashboard_view
    return dashboard_view(request)


def get_participant(token):
    return get_object_or_404(Participant.objects.select_related('team__tournament', 'payment'), token=token)


def state_version(team, participants):
    state = {'team': (team.status, team.name, team.contact) if team else None, 'roster': str(team.roster_checked_at) if team else None, 'rejection': team.rejection_reason if team else None, 'players': [(p.pk, p.active, p.account_id, p.nickname, p.is_reserve, p.registration_confirmed, p.payment.required, p.payment.amount, p.payment.status, str(p.payment.reviewed_at), p.payment.rejection_reason) for p in participants]}
    return hashlib.sha256(json.dumps(state, ensure_ascii=True).encode()).hexdigest()


@login_required
def captain_state(request):
    from .team_views import user_team
    team = user_team(request.user, get_tournament())
    players = list(team.participants.filter(active=True).select_related('payment').order_by('pk')) if team else []
    return JsonResponse({'version': state_version(team, players)})


def participant_state(request, token):
    player = get_participant(token)
    return JsonResponse({'version': state_version(player.team, [player])})


def participant(request, token):
    from .game_accounts import has_telegram
    from .models import GameAccount
    player = get_participant(token)
    checkout = ManualKaspiProvider().create_checkout(player.payment)
    profile = GameAccount.objects.filter(user=request.user).first() if request.user.is_authenticated else None
    return render(request, 'arena/participant.html', {'participant': player, 'game_account': profile, 'telegram_linked': has_telegram(request.user), 'account_matches': bool(profile and (profile.game_id, profile.server_id) == (player.game_id, player.server_id)), 'payment': player.payment, 'checkout': checkout, 'team': player.team, 'tournament': player.team.tournament, 'payment_code': player.payment.code, 'live_status_url': reverse('participant_state', args=[token]), 'live_status_version': state_version(player.team, [player])})


@require_POST
def participant_confirm(request, token):
    from .game_accounts import participant_claim
    return participant_claim(request, token)


@require_POST
@login_required
def payment_submit(request, token):
    from .game_accounts import has_telegram
    player = get_participant(token)
    if player.account_id != request.user.pk or not has_telegram(request.user):
        raise PermissionDenied('Сообщить об оплате может только участник, связавший свою заявку с Telegram.')
    try:
        services.submit_payment(player)
        messages.success(request, 'Взнос отправлен на проверку кассиру. Оплата пока не подтверждена.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('participant', token=token)


@staff_only
def organizer(request):
    tournament = get_tournament()
    payments = Payment.objects.filter(participant__tournament=tournament)
    totals = {'pending_payments': payments.filter(status='review').count(), 'confirmed_payments': payments.filter(status='confirmed').count(), 'confirmed_amount': payments.filter(status='confirmed').aggregate(value=Sum('amount'))['value'] or 0, 'confirmed_teams': tournament.teams.filter(status='confirmed').count()}
    return render(request, 'arena/organizer.html', {'pending_payments': payments.filter(status='review').select_related('participant__team').order_by('submitted_at'), 'teams': tournament.teams.select_related('captain').prefetch_related('participants__payment').order_by('created_at'), 'matches': match_queryset(), 'totals': totals, 'recent_audits': PaymentAudit.objects.filter(payment__participant__tournament=tournament).select_related('payment__participant__team', 'actor_user').order_by('-created_at')[:20], 'registration_errors': services.registration_errors(tournament)})


@staff_only
def review_payment(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related('participant__team'), pk=payment_id)
    form = PaymentReviewForm(request.POST or None, initial={'submission': payment.submitted_at.isoformat() if payment.submitted_at else ''})
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                current = Payment.objects.select_for_update().get(pk=payment_id)
                if not current.submitted_at or form.cleaned_data['submission'] != current.submitted_at.isoformat():
                    raise ValidationError('Заявка изменилась. Откройте её заново перед решением.')
                services.review_payment(payment_id, form.cleaned_data['decision'] == 'approve', form.cleaned_data['bank_reference'], form.cleaned_data['reason'], actor_user=request.user)
            messages.success(request, 'Решение сохранено в журнале проверок.')
            return redirect('organizer')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'arena/review.html', {'payment': payment, 'payment_code': payment.code, 'form': form})


@require_POST
@staff_only
def roster_check(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        services.check_roster(team, request.user)
        messages.success(request, 'Состав проверен.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('organizer')


@require_POST
@staff_only
def team_confirm(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        services.confirm_team(team, request.user)
        messages.success(request, 'Команда допущена к турниру.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('organizer')


@require_POST
@staff_only
def team_reject(request, team_id):
    from django.contrib.admin.models import LogEntry, CHANGE
    from django.contrib.contenttypes.models import ContentType
    team = get_object_or_404(Team, pk=team_id)
    reason = request.POST.get('reason', '').strip()
    try:
        with transaction.atomic():
            Tournament.objects.select_for_update().get(pk=team.tournament_id)
            team = Team.objects.select_for_update().get(pk=team_id)
            if not reason or len(reason) > 1000:
                raise ValidationError('Укажите причину отклонения (до 1000 символов).')
            if team.tournament.matches.exists():
                raise ValidationError('После формирования сетки отклонить команду нельзя.')
            team.status = Team.Status.REJECTED
            team.rejection_reason = reason
            team.save(update_fields=['status', 'rejection_reason'])
            services.notify_team_status(team, 'Заявка команды отклонена', reason)
            LogEntry.objects.log_actions(user_id=request.user.pk, queryset=Team.objects.filter(pk=team.pk), action_flag=CHANGE, change_message='Отклонение команды: ' + reason)
        messages.success(request, 'Команда отклонена. Оплаты и история сохранены; возвраты выполняются отдельно в Kaspi Pay.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('organizer')


@require_POST
@staff_only
def team_reopen(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        services.reopen_team(team, request.user, request.POST.get('reason', ''))
        messages.success(request, 'Команда возвращена на сбор состава. Капитан сможет подать заявку повторно; платежи и история сохранены.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('organizer')


@require_POST
@staff_only
def generate_bracket(request):
    try:
        if request.POST.get('confirm_generate') not in ('on', '1', 'true', 'yes'):
            raise ValidationError('Подтвердите формирование сетки.')
        services.generate_bracket(get_tournament(), request.user)
        messages.success(request, 'Сетка сформирована. Регистрация закрыта. Укажите расписание матчей.')
    except ValidationError as exc:
        domain_error(request, exc)
    return redirect('organizer')


def match_edit(request, match_id):
    from .match_views import edit
    return edit(request, match_id)



def forbidden(request, exception=None):
    return render(request, 'arena/error.html', {'title': 'Нет доступа', 'detail': 'Этот раздел доступен только уполномоченному организатору.'}, status=403)

def not_found(request, exception=None):
    return render(request, 'arena/error.html', {'title': 'Страница не найдена', 'detail': 'Проверьте ссылку. Личная ссылка участника должна быть полной.'}, status=404)

def server_error(request):
    return render(request, 'arena/error.html', {'title': 'Не удалось выполнить запрос', 'detail': 'Повторите попытку позже. Если ошибка сохраняется, обратитесь к организатору.'}, status=500)
