"""Serialized match-day actions. Claims never become official results automatically."""
from datetime import datetime
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Match, MatchAudit, MatchReport, Team, Tournament
from .services import _staff


def _snapshot(match):
    fields = ("team_a_id", "team_b_id", "scheduled_at", "ready_deadline", "ready_a_at",
              "ready_b_at", "started_at", "lobby_details", "score_a", "score_b",
              "winner_id", "completed_at", "resolution", "resolution_reason", "active")
    return {name: value.isoformat() if isinstance(value := getattr(match, name), datetime) else value for name in fields}


def _audit(match, actor, action, before, reason=""):
    return MatchAudit.objects.create(match=match, actor=actor, action=action, before=before,
                                     after=_snapshot(match), reason=reason)


def _notify(match, audit, title):
    from .notifications import notify_match
    notify_match(match, event_key=f"match:{match.pk}:{audit.action}:{audit.pk}", title=f"{title} · {match.key}")


def _lock(match):
    expected_teams = (match.team_a_id, match.team_b_id)
    Tournament.objects.select_for_update().get(pk=match.tournament_id)
    locked = Match.objects.select_for_update(of=("self",)).select_related("source_a", "source_b", "team_a", "team_b").get(pk=match.pk)
    if (locked.team_a_id, locked.team_b_id) != expected_teams:
        raise ValidationError("Участники матча изменились. Обновите страницу и проверьте соперника перед повторным действием.")
    return locked


def _fresh_staff(actor_user):
    _staff(actor_user)
    if not get_user_model().objects.filter(pk=actor_user.pk, is_active=True, is_staff=True).exists():
        raise PermissionDenied("Права организатора изменились. Войдите повторно.")


def captain_team(match, actor_user):
    if not actor_user or not actor_user.is_authenticated or not actor_user.is_active:
        raise PermissionDenied("Требуется вход капитана команды.")
    if not get_user_model().objects.filter(pk=actor_user.pk, is_active=True).exists():
        raise PermissionDenied("Учётная запись отключена.")
    for team in (match.team_a, match.team_b):
        if team and team.status == Team.Status.CONFIRMED and team.captain_id == actor_user.pk:
            return team
    raise PermissionDenied("Действие доступно только капитану одной из команд матча.")


def _playable(match):
    from .bracket import _source_outcome
    if match.completed_at or match.winner_id:
        raise ValidationError("Матч уже завершён.")
    if not match.active or not match.team_a_id or not match.team_b_id:
        raise ValidationError("Участники матча ещё не определены.")
    if match.team_a_id == match.team_b_id:
        raise ValidationError("Команда не может играть сама с собой.")
    if match.team_a.status != Team.Status.CONFIRMED or match.team_b.status != Team.Status.CONFIRMED:
        raise ValidationError("Обе команды должны быть допущены к турниру.")
    for side in ("a", "b"):
        source = getattr(match, f"source_{side}")
        if source and (not source.completed_at or getattr(match, f"team_{side}_id") != _source_outcome(source, getattr(match, f"source_{side}_outcome"))):
            raise ValidationError("Сначала завершите предыдущие матчи и определите участников.")


def _reason(value, max_length=1000):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValidationError(f"Укажите причину длиной от 1 до {max_length} символов.")
    return value.strip()


def _scores(match, score_a, score_b):
    if any(isinstance(score, bool) or not isinstance(score, int) or score < 0 for score in (score_a, score_b)):
        raise ValidationError("Счёт должен состоять из неотрицательных целых чисел.")
    required = match.best_of // 2 + 1
    if match.best_of < 1 or match.best_of % 2 != 1 or not ((score_a == required and score_b < required) or (score_b == required and score_a < required)):
        raise ValidationError(f"Для BO{match.best_of} победитель должен выиграть ровно {required} карт.")


@transaction.atomic
def update_match_details(match, actor_user, *, scheduled_at, ready_deadline, lobby_details):
    _staff(actor_user)
    match = _lock(match)
    _fresh_staff(actor_user)
    if match.completed_at or match.started_at:
        raise ValidationError("Параметры начатого или завершённого матча менять нельзя.")
    if not match.active:
        raise ValidationError("Этот матч пока не требуется по результатам сетки.")
    for value in (scheduled_at, ready_deadline):
        if value is not None and (not isinstance(value, datetime) or timezone.is_naive(value)):
            raise ValidationError("Время должно содержать часовой пояс.")
    if not isinstance(lobby_details, str) or len(lobby_details) > 2000:
        raise ValidationError("Инструкция лобби: не более 2000 символов.")
    if scheduled_at:
        if match.tournament.starts_at and scheduled_at < match.tournament.starts_at:
            raise ValidationError("Матч не может начинаться раньше турнира.")
        for source in (match.source_a, match.source_b):
            if source and source.scheduled_at and scheduled_at <= source.scheduled_at:
                raise ValidationError("Матч должен начинаться после предыдущих матчей сетки.")
        if Match.objects.filter(Q(source_a=match) | Q(source_b=match), scheduled_at__lte=scheduled_at).exists():
            raise ValidationError("Следующий матч уже запланирован раньше этого времени. Сначала перенесите следующий матч.")
        team_ids = [value for value in (match.team_a_id, match.team_b_id) if value]
        if team_ids and Match.objects.filter(tournament_id=match.tournament_id, active=True, scheduled_at=scheduled_at).filter(Q(team_a_id__in=team_ids) | Q(team_b_id__in=team_ids)).exclude(pk=match.pk).exists():
            raise ValidationError("У команды уже есть матч на это время.")
    before = _snapshot(match)
    if match.scheduled_at != scheduled_at or match.ready_deadline != ready_deadline:
        match.ready_a_at = match.ready_b_at = None
    match.scheduled_at, match.ready_deadline = scheduled_at, ready_deadline
    match.lobby_details = lobby_details.strip()
    if before == _snapshot(match):
        return match
    match.save(update_fields=["scheduled_at", "ready_deadline", "lobby_details", "ready_a_at", "ready_b_at"])
    audit = _audit(match, actor_user, "details", before)
    _notify(match, audit, "Обновлены время или условия матча")
    return match


@transaction.atomic
def mark_ready(match, actor_user):
    match = _lock(match)
    team = captain_team(match, actor_user)
    _playable(match)
    if match.started_at:
        raise ValidationError("Матч уже начался.")
    side = "a" if match.team_a_id == team.pk else "b"
    field = f"ready_{side}_at"
    if getattr(match, field):
        return match
    if match.ready_deadline and timezone.now() > match.ready_deadline:
        raise ValidationError("Срок готовности истёк. Свяжитесь с судьёй; техническое поражение автоматически не назначается.")
    before = _snapshot(match)
    setattr(match, field, timezone.now())
    match.save(update_fields=[field])
    audit = _audit(match, actor_user, "ready", before)
    _notify(match, audit, "Команда подтвердила готовность к матчу")
    return match


@transaction.atomic
def start_match(match, actor_user):
    _staff(actor_user)
    match = _lock(match)
    _fresh_staff(actor_user)
    _playable(match)
    if match.started_at:
        return match
    if not match.ready_a_at or not match.ready_b_at:
        raise ValidationError("Оба капитана должны подтвердить готовность.")
    before = _snapshot(match)
    match.started_at = timezone.now()
    match.save(update_fields=["started_at"])
    audit = _audit(match, actor_user, "start", before)
    _notify(match, audit, "Матч начался")
    return match


@transaction.atomic
def record_forfeit(match, winner_team_id, actor_user, reason):
    from .bracket import _advance
    _staff(actor_user)
    reason = _reason(reason)
    match = _lock(match)
    _fresh_staff(actor_user)
    _playable(match)
    if isinstance(winner_team_id, bool) or winner_team_id not in (match.team_a_id, match.team_b_id):
        raise ValidationError("Победителем должна быть одна из команд матча.")
    before = _snapshot(match)
    match.winner_id = winner_team_id
    match.score_a = match.score_b = None
    match.resolution = Match.Resolution.FORFEIT
    match.resolution_reason = reason
    match.completed_at = timezone.now()
    match.save(update_fields=["winner", "score_a", "score_b", "resolution", "resolution_reason", "completed_at"])
    _advance(match.tournament_id, actor_user)
    audit = _audit(match, actor_user, "forfeit", before, reason)
    _notify(match, audit, "Судья назначил техническое поражение")
    return match


def _descendants(match, all_matches):
    affected = {match.pk}
    descendants = []
    # Source references form a DAG in generated brackets; iteration also works
    # when imported rows do not follow insertion order.
    while True:
        new = [node for node in all_matches if node.pk not in affected and (node.source_a_id in affected or node.source_b_id in affected)]
        if not new:
            return descendants
        descendants.extend(new)
        affected.update(node.pk for node in new)


@transaction.atomic
def _correct_outcome(match, score_a, score_b, actor_user, reason, *, winner_team_id=None):
    from .bracket import _advance
    from .notifications import notify_teams
    _staff(actor_user)
    reason = _reason(reason)
    match = _lock(match)
    _fresh_staff(actor_user)
    if not match.completed_at or match.resolution not in (Match.Resolution.PLAYED, Match.Resolution.FORFEIT):
        raise ValidationError("Исправить можно только сыгранный матч или техническое поражение.")
    if winner_team_id is None:
        _scores(match, score_a, score_b)
    elif isinstance(winner_team_id, bool) or winner_team_id not in (match.team_a_id, match.team_b_id):
        raise ValidationError("Победителем должна быть одна из команд матча.")
    all_matches = list(Match.objects.select_for_update().filter(tournament_id=match.tournament_id).order_by("pk"))
    descendants = _descendants(match, all_matches)
    if any(node.started_at or node.ready_a_at or node.ready_b_at or node.resolution in (Match.Resolution.PLAYED, Match.Resolution.FORFEIT) for node in descendants):
        raise ValidationError("Исправление заблокировано: в зависимом матче уже есть готовность, начало или результат.")
    if MatchReport.objects.filter(match_id__in=[node.pk for node in descendants]).exists():
        raise ValidationError("Исправление заблокировано: по зависимому матчу уже подано обращение.")
    before = _snapshot(match)
    descendant_before = {node.pk: _snapshot(node) for node in descendants}
    for node in descendants:
        for side in ("a", "b"):
            if getattr(node, f"source_{side}_id"):
                setattr(node, f"team_{side}_id", None)
        node.score_a = node.score_b = node.winner_id = node.completed_at = None
        node.resolution = node.resolution_reason = node.lobby_details = ""
        if node.bracket == Match.Bracket.RESET:
            node.active = False
        node.save(update_fields=["team_a", "team_b", "score_a", "score_b", "winner", "completed_at", "resolution", "resolution_reason", "lobby_details", "active"])
    match.score_a, match.score_b = score_a, score_b
    match.winner_id = winner_team_id if winner_team_id is not None else (match.team_a_id if score_a > score_b else match.team_b_id)
    match.resolution = Match.Resolution.FORFEIT if winner_team_id is not None else Match.Resolution.PLAYED
    match.resolution_reason = reason
    match.completed_at = timezone.now()
    match.save(update_fields=["score_a", "score_b", "winner", "resolution", "resolution_reason", "completed_at"])
    _advance(match.tournament_id)
    audit = _audit(match, actor_user, "correction", before, reason)
    _notify(match, audit, "Судья исправил результат матча")
    for node in descendants:
        node.refresh_from_db()
        if descendant_before[node.pk] != _snapshot(node):
            child_audit = _audit(node, actor_user, "recalculated", descendant_before[node.pk], f"Исправлен матч {match.key}: {reason}"[:2000])
            notify_teams([node.team_a_id, node.team_b_id, descendant_before[node.pk]["team_a_id"], descendant_before[node.pk]["team_b_id"]],
                         event_key=f"match:{node.pk}:recalculated:{child_audit.pk}", title="Состав матча пересчитан после решения судьи", path=f"/matches/{node.pk}/")
    return match


def correct_result(match, score_a, score_b, actor_user, reason):
    return _correct_outcome(match, score_a, score_b, actor_user, reason)


def correct_forfeit(match, winner_team_id, actor_user, reason):
    if winner_team_id is None:
        raise ValidationError("Укажите победившую команду.")
    return _correct_outcome(match, None, None, actor_user, reason, winner_team_id=winner_team_id)


@transaction.atomic
def submit_report(match, actor_user, *, kind, message, evidence_url="", reported_score_a=None, reported_score_b=None):
    match = _lock(match)
    team = captain_team(match, actor_user)
    if not match.active or not match.team_a_id or not match.team_b_id or match.resolution in (Match.Resolution.BYE, Match.Resolution.EMPTY):
        raise ValidationError("Обращение доступно только для матча с двумя командами.")
    if kind not in MatchReport.Kind.values:
        raise ValidationError("Неизвестный тип обращения.")
    message = _reason(message, 2000)
    if not isinstance(evidence_url, str) or len(evidence_url) > 1000:
        raise ValidationError("Некорректная ссылка на доказательство.")
    evidence_url = evidence_url.strip()
    if evidence_url:
        URLValidator(schemes=["https"])(evidence_url)
        parsed = urlsplit(evidence_url)
        if parsed.username or parsed.password:
            raise ValidationError("Ссылка не должна содержать логин и пароль.")
    if kind == MatchReport.Kind.RESULT:
        _scores(match, reported_score_a, reported_score_b)
    elif reported_score_a is not None or reported_score_b is not None:
        raise ValidationError("Для обращения к судье счёт заполнять не нужно.")
    if MatchReport.objects.filter(match=match, team=team, kind=kind, status=MatchReport.Status.PENDING).exists():
        raise ValidationError("Обращение этого типа уже ожидает рассмотрения.")
    report = MatchReport.objects.create(match=match, team=team, submitted_by=actor_user, kind=kind,
        message=message, evidence_url=evidence_url, reported_score_a=reported_score_a, reported_score_b=reported_score_b)
    _audit(match, actor_user, "report", _snapshot(match), f"Обращение #{report.pk}: {report.get_kind_display()}")
    return report


@transaction.atomic
def review_report(report, actor_user, *, status, reason):
    from .notifications import notify_teams
    _staff(actor_user)
    reason = _reason(reason)
    match = _lock(report.match)
    _fresh_staff(actor_user)
    report = MatchReport.objects.select_for_update().get(pk=report.pk)
    if report.status != MatchReport.Status.PENDING:
        raise ValidationError("Обращение уже рассмотрено.")
    if status not in (MatchReport.Status.RESOLVED, MatchReport.Status.REJECTED):
        raise ValidationError("Выберите итог рассмотрения.")
    report.status, report.decision_reason = status, reason
    report.reviewed_by, report.reviewed_at = actor_user, timezone.now()
    report.save(update_fields=["status", "decision_reason", "reviewed_by", "reviewed_at"])
    _audit(match, actor_user, "report_review", _snapshot(match), f"Обращение #{report.pk}: {reason}"[:2000])
    notify_teams([report.team_id], event_key=f"report:{report.pk}:review", title="Судья рассмотрел обращение", path=f"/matches/{match.pk}/")
    return report
