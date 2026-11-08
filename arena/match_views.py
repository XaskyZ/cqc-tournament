"""Public match results and a private workspace for the assigned teams."""
import hashlib
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import match_services, services
from .match_forms import CorrectionForm, ForfeitForm, MatchSettingsForm, ReportDecisionForm, ReportForm, ResultForm
from .models import Match, MatchAudit, MatchReport, Participant, Team, Tournament
from .views import staff_only


def get_match(match_id):
    return get_object_or_404(Match.objects.select_related('tournament', 'team_a__captain', 'team_b__captain', 'winner', 'source_a', 'source_b'), pk=match_id)


def _match_fingerprint(match):
    # Readiness of the other side and new reports do not invalidate a captain's
    # open page. Opponent, schedule, lobby or official result changes do.
    fields = ('team_a_id', 'team_b_id', 'scheduled_at', 'ready_deadline', 'lobby_details',
        'started_at', 'completed_at', 'score_a', 'score_b', 'winner_id', 'resolution', 'active')
    payload = json.dumps([getattr(match, field) for field in fields], default=str, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def match_revision(match):
    return signing.dumps({'match': match.pk, 'state': _match_fingerprint(match)}, salt='cqc.match.revision')


def _check_revision(request, match):
    try:
        value = signing.loads(request.POST.get('revision', ''), salt='cqc.match.revision', max_age=86400)
    except signing.BadSignature:
        raise ValidationError('Форма устарела. Обновите страницу и проверьте участников и результат перед повторной отправкой.')
    if value != {'match': match.pk, 'state': _match_fingerprint(match)}:
        raise ValidationError('Матч изменился. Обновите страницу и проверьте участников, время и результат перед повторной отправкой.')


def access_for(match, user):
    if not user.is_authenticated or not user.is_active:
        return False, None
    teams = [team for team in (match.team_a, match.team_b) if team]
    captain_team = next((team for team in teams if team.captain_id == user.pk and team.status == Team.Status.CONFIRMED), None)
    allowed = user.is_staff or captain_team is not None or Participant.objects.filter(
        team_id__in=[team.pk for team in teams], account=user, active=True, team__status=Team.Status.CONFIRMED).exists()
    return allowed, captain_team


def detail_context(request, match, report_form=None):
    private_access, captain_team = access_for(match, request.user)
    reports = MatchReport.objects.filter(match=match).select_related('team', 'submitted_by', 'reviewed_by').order_by('-created_at') if private_access else []
    if private_access and not request.user.is_staff:
        own_teams = Participant.objects.filter(account=request.user, active=True, team_id__in=[match.team_a_id, match.team_b_id]).values_list('team_id', flat=True)
        team_ids = set(own_teams)
        if captain_team:
            team_ids.add(captain_team.pk)
        reports = reports.filter(team_id__in=team_ids)
    is_ready = bool(captain_team and ((match.team_a_id == captain_team.pk and match.ready_a_at) or (match.team_b_id == captain_team.pk and match.ready_b_at)))
    can_ready = bool(captain_team and match.active and match.team_a_id and match.team_b_id and not match.completed_at and not match.started_at and not is_ready)
    return {'match': match, 'match_revision': match_revision(match), 'private_access': private_access, 'captain_team': captain_team,
        'can_ready': can_ready, 'reports': reports,
        'report_form': report_form or ReportForm(match=match, auto_id='id_report_%s'),
        'ready_expired': bool(match.ready_deadline and match.ready_deadline < timezone.now() and not match.completed_at),
        'can_report': bool(captain_team and match.active and match.team_a_id and match.team_b_id and match.resolution not in ('bye', 'empty'))}


@never_cache
@require_GET
def detail(request, match_id):
    return render(request, 'arena/match_detail.html', detail_context(request, get_match(match_id)))


@never_cache
@require_POST
@login_required
@transaction.atomic
def action(request, match_id):
    match = get_match(match_id)
    Tournament.objects.select_for_update().get(pk=match.tournament_id)
    match = get_match(match_id)
    _, captain_team = access_for(match, request.user)
    if not captain_team:
        raise PermissionDenied('Действие доступно только капитану одной из команд матча.')
    kind = request.POST.get('action')
    if kind not in ('ready', 'report'):
        raise PermissionDenied('Недопустимое действие.')
    form = ReportForm(request.POST, match=match, auto_id='id_report_%s') if kind == 'report' else None
    try:
        _check_revision(request, match)
        if kind == 'ready':
            match_services.mark_ready(match, request.user)
            messages.success(request, 'Готовность вашей команды подтверждена.')
        elif form.is_valid():
            match_services.submit_report(match, request.user, **form.cleaned_data)
            messages.success(request, 'Обращение передано судье. Официальный результат изменяет организатор.')
        else:
            return render(request, 'arena/match_detail.html', detail_context(request, match, form))
    except ValidationError as exc:
        if form is not None:
            form.add_error(None, exc)
            return render(request, 'arena/match_detail.html', detail_context(request, match, form))
        for message in exc.messages:
            messages.error(request, message)
    return redirect(reverse('match_detail', args=[match_id]) + ('#reports' if kind == 'report' else '#readiness'))


def edit_forms(match):
    return {
        'settings_form': MatchSettingsForm(initial={'scheduled_at': timezone.localtime(match.scheduled_at) if match.scheduled_at else None,
            'ready_deadline': timezone.localtime(match.ready_deadline) if match.ready_deadline else None,
            'lobby_details': match.lobby_details}, auto_id='id_settings_%s'),
        'result_form': ResultForm(match=match, auto_id='id_result_%s'),
        'forfeit_form': ForfeitForm(match=match, auto_id='id_forfeit_%s'),
        'correction_form': CorrectionForm(match=match, auto_id='id_correction_%s', initial={'score_a': match.score_a, 'score_b': match.score_b}),
    }


@never_cache
@staff_only
@transaction.atomic
def edit(request, match_id):
    match = get_match(match_id)
    if request.method == 'POST':
        Tournament.objects.select_for_update().get(pk=match.tournament_id)
        match = get_match(match_id)
    forms = edit_forms(match)
    if request.method == 'POST':
        operation = request.POST.get('action', 'settings')
        form_map = {'settings': ('settings_form', MatchSettingsForm), 'result': ('result_form', ResultForm),
            'forfeit': ('forfeit_form', ForfeitForm), 'correct_forfeit': ('forfeit_form', ForfeitForm), 'correct': ('correction_form', CorrectionForm)}
        if operation not in (*form_map, 'start'):
            raise PermissionDenied('Недопустимое действие.')
        form = None
        if operation in form_map:
            name, cls = form_map[operation]
            form = cls(request.POST, auto_id=f'id_{operation}_%s', **({'match': match} if operation != 'settings' else {}))
            forms[name] = form
        if form is None or form.is_valid():
            try:
                _check_revision(request, match)
                if operation == 'settings':
                    with transaction.atomic():
                        match_services.update_match_details(match, request.user, **form.cleaned_data)
                        # Preserve submissions from the previous combined schedule/result form.
                        if 'action' not in request.POST and (request.POST.get('score_a') or request.POST.get('score_b')):
                            legacy_result = ResultForm(request.POST, match=match)
                            if not legacy_result.is_valid():
                                raise ValidationError('Для результата заполните корректный счёт обеих команд.')
                            services.record_result(match, **legacy_result.cleaned_data, actor_user=request.user)
                elif operation == 'result':
                    services.record_result(match, **form.cleaned_data, actor_user=request.user)
                elif operation == 'start':
                    match_services.start_match(match, request.user)
                elif operation == 'forfeit':
                    match_services.record_forfeit(match, actor_user=request.user, **form.cleaned_data)
                elif operation == 'correct_forfeit':
                    match_services.correct_forfeit(match, actor_user=request.user, **form.cleaned_data)
                elif operation == 'correct':
                    match_services.correct_result(match, actor_user=request.user, **form.cleaned_data)
                messages.success(request, 'Изменения матча сохранены в журнале.')
                return redirect(reverse('match_edit', args=[match_id]) + '#match-management')
            except ValidationError as exc:
                for message in exc.messages:
                    messages.error(request, message)
                if form is not None:
                    form.add_error(None, exc)
    context = detail_context(request, match)
    context.update(forms)
    context['audits'] = MatchAudit.objects.filter(match=match).select_related('actor').order_by('-created_at')[:30]
    action_labels = {'ready': 'Подтверждена готовность', 'start': 'Матч начат', 'details': 'Изменены параметры',
        'result': 'Зафиксирован результат', 'forfeit': 'Техническое поражение', 'correct': 'Исправлен результат',
        'report': 'Подано обращение', 'report_review': 'Решение по обращению', 'correction': 'Исправлен результат',
        'correct_forfeit': 'Исправлено техническое поражение', 'recalculated': 'Пересчитаны участники',
        'advancement': 'Определены участники следующего матча'}
    for audit in context['audits']:
        audit.display_action = action_labels.get(audit.action, audit.action)
    return render(request, 'arena/match_edit.html', context)


@never_cache
@staff_only
@require_GET
def report_queue(request):
    reports = MatchReport.objects.filter(status='pending').select_related('match__team_a', 'match__team_b', 'team').order_by('created_at')
    return render(request, 'arena/match_reports.html', {'reports': reports})


@never_cache
@require_POST
@staff_only
def report_review(request, report_id):
    report = get_object_or_404(MatchReport.objects.select_related('match', 'team'), pk=report_id)
    form = ReportDecisionForm(request.POST)
    if form.is_valid():
        try:
            match_services.review_report(report, request.user, **form.cleaned_data)
            messages.success(request, 'Решение по обращению сохранено. Счёт матча меняется отдельно.')
        except ValidationError as exc:
            form.add_error(None, exc)
    if form.errors:
        return render(request, 'arena/match_report_review.html', {'report': report, 'form': form})
    return redirect(reverse('match_edit', args=[report.match_id]) + '#reports')
