from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Outbox, Participant, Payment, PaymentAudit, Team, TelegramIdentity, Tournament


def _staff(actor):
    if not actor or not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise PermissionDenied("Действие доступно только организатору.")


def notify_team_status(team, title, body=''):
    import uuid
    from .notifications import notify_teams
    notify_teams([team.pk], event_key=f'team:{team.pk}:{uuid.uuid4()}', title=title,
                 body=body, path='/captain/')


def _reviewer(actor_user, actor_telegram):
    if actor_user is not None:
        _staff(actor_user)
        return
    cashier_id = getattr(settings, "TELEGRAM_CASHIER_ID", None)
    if not cashier_id or isinstance(actor_telegram, bool) or str(actor_telegram) != str(cashier_id):
        raise PermissionDenied("Проверять оплату может только назначенный кассир.")


def registration_errors(tournament):
    errors = []
    try:
        tournament.clean()
    except ValidationError as exc:
        errors.extend(exc.messages)
    if not tournament.terms_confirmed:
        errors.append("Организатор ещё не подтвердил условия турнира.")
    if not tournament.registration_open:
        errors.append("Регистрация закрыта.")
    if not tournament.rules.strip():
        errors.append("Правила турнира ещё не опубликованы.")
    if not tournament.starts_at or not tournament.end_registration:
        errors.append("Время начала и срок регистрации ещё не указаны.")
    if tournament.starts_at and tournament.end_registration and tournament.end_registration >= tournament.starts_at:
        errors.append("Регистрация должна завершаться до начала турнира.")
    if not tournament.timezone_name:
        errors.append("Часовой пояс турнира ещё не указан.")
    else:
        try:
            zone = ZoneInfo(tournament.timezone_name)
            if tournament.starts_at and timezone.is_aware(tournament.starts_at) and timezone.localtime(tournament.starts_at, zone).date() != tournament.event_date:
                errors.append("Дата и время начала турнира не согласованы.")
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("Некорректный часовой пояс турнира.")
    for field, label in [("reserve_limit", "Количество запасных"), ("reserves_pay", "Оплата запасных"), ("best_of", "Формат матчей"), ("final_best_of", "Формат финала"), ("grand_final_reset", "Повторный гранд-финал")]:
        if getattr(tournament, field) is None:
            errors.append(f"{label}: требуется решение организатора.")
    for value in [tournament.best_of, tournament.final_best_of]:
        if value is not None and (value < 1 or value % 2 != 1):
            errors.append("Формат best-of должен быть положительным нечётным числом.")
    if tournament.max_teams < 2 or tournament.roster_size < 1:
        errors.append("Некорректный размер турнира или состава.")
    if tournament.end_registration and timezone.now() >= tournament.end_registration:
        errors.append("Срок регистрации истёк.")
    if tournament.starts_at and timezone.now() >= tournament.starts_at:
        errors.append("Турнир уже начался.")
    if tournament.matches.exists():
        errors.append("Сетка уже сформирована.")
    if tournament.pk and tournament.teams.filter(status__in=[Team.Status.PENDING, Team.Status.CONFIRMED]).count() >= tournament.max_teams:
        errors.append("Все места в турнире заняты.")
    return errors


def _text(value, label, max_length):
    value = str(value or "").strip()
    if not value or len(value) > max_length:
        raise ValidationError(f"{label}: укажите от 1 до {max_length} символов.")
    return value


@transaction.atomic
def register_team(tournament, captain, name, contact, players):
    if not captain or not captain.is_authenticated or not captain.is_active:
        raise PermissionDenied("Для регистрации требуется войти в аккаунт.")
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    errors = registration_errors(tournament)
    if errors:
        raise ValidationError(errors)
    if Team.objects.filter(tournament=tournament, captain=captain).exists():
        raise ValidationError("У капитана уже есть команда в этом турнире.")
    name = _text(name, "Название команды", 100)
    if any(existing.casefold() == name.casefold() for existing in tournament.teams.values_list("name", flat=True)):
        raise ValidationError("Команда с таким названием уже зарегистрирована.")
    if tournament.teams.filter(status__in=[Team.Status.PENDING, Team.Status.CONFIRMED]).count() >= tournament.max_teams:
        raise ValidationError("Все места заняты.")
    if not isinstance(players, (list, tuple)) or not players:
        raise ValidationError("Добавьте игроков команды.")
    normalized = []
    for player in players:
        if not isinstance(player, dict) or not isinstance(player.get("is_reserve", False), bool):
            raise ValidationError("Некорректные данные игрока.")
        normalized.append({
            "nickname": _text(player.get("nickname"), "Никнейм", 100),
            "game_id": _text(player.get("game_id"), "Game ID", 40),
            "server_id": _text(player.get("server_id"), "Server ID", 40),
            "is_reserve": player.get("is_reserve", False),
        })
    if sum(not p["is_reserve"] for p in normalized) != tournament.roster_size:
        raise ValidationError(f"Основной состав должен содержать {tournament.roster_size} игроков.")
    if sum(p["is_reserve"] for p in normalized) > tournament.reserve_limit:
        raise ValidationError("Превышено разрешённое количество запасных.")
    identities = {(p["game_id"], p["server_id"]) for p in normalized}
    if len(identities) != len(normalized):
        raise ValidationError("Игрок не может быть указан дважды.")
    for game_id, server_id in identities:
        if Participant.objects.filter(active=True, tournament=tournament, game_id=game_id, server_id=server_id).exists():
            raise ValidationError("Игрок с таким Game ID и Server ID уже зарегистрирован.")
    team = Team.objects.create(tournament=tournament, captain=captain, name=_text(name, "Название команды", 100), contact=_text(contact, "Контакт капитана", 200))
    for player in normalized:
        participant = Participant.objects.create(team=team, **player)
        required = not participant.is_reserve or tournament.reserves_pay
        Payment.objects.create(participant=participant, required=required, amount=2200 if required else 0)
    return team


def _locked_participant(participant):
    # Match membership/recovery lock order. FOR UPDATE on select_related alone
    # would also lock joined rows, with backend-dependent acquisition order.
    tournament_id, team_id = Participant.objects.values_list("tournament_id", "team_id").get(pk=participant.pk)
    Tournament.objects.select_for_update().get(pk=tournament_id)
    team = Team.objects.select_for_update().get(pk=team_id)
    current = Participant.objects.select_for_update().get(pk=participant.pk)
    current.team = team
    return current


@transaction.atomic
def confirm_registration(participant):
    participant = _locked_participant(participant)
    if not participant.active:
        raise ValidationError('Это участие завершено. Используйте новое приглашение команды.')
    if participant.team.status == Team.Status.REJECTED:
        raise ValidationError("Команда отклонена организатором.")
    if not participant.registration_confirmed:
        participant.registration_confirmed = True
        participant.save(update_fields=["registration_confirmed"])
    return participant


@transaction.atomic
def submit_payment(participant):
    participant = _locked_participant(participant)
    if not participant.active:
        raise ValidationError('Участник выведен из состава. Новые взносы по этой заявке недоступны.')
    payment = Payment.objects.select_for_update().get(participant=participant)
    if participant.team.status == Team.Status.DRAFT:
        raise ValidationError('Взнос будет доступен после подачи команды на турнир.')
    if participant.team.status == Team.Status.REJECTED:
        raise ValidationError("Команда отклонена организатором.")
    if not participant.registration_confirmed:
        raise ValidationError("Сначала подтвердите участие.")
    if not payment.required:
        raise ValidationError("Для этого участника оплата не требуется.")
    if payment.status == Payment.Status.REVIEW:
        return payment
    if payment.status == Payment.Status.CONFIRMED:
        raise ValidationError("Оплата уже подтверждена кассиром.")
    previous = payment.status
    payment.status = Payment.Status.REVIEW
    payment.submitted_at = timezone.now()
    payment.rejection_reason = ""
    payment.reviewed_at = None
    payment.reviewed_by = None
    payment.reviewed_by_telegram = None
    payment.save(update_fields=["status", "submitted_at", "rejection_reason", "reviewed_at", "reviewed_by", "reviewed_by_telegram"])
    PaymentAudit.objects.create(payment=payment, from_status=previous, to_status=payment.status, reason="Участник сообщил об оплате; требуется сверка с банком.")
    Outbox.objects.create(payment=payment, kind="review")
    return payment


@transaction.atomic
def review_payment(payment_id, approve, bank_reference="", reason="", actor_user=None, actor_telegram=None):
    _reviewer(actor_user, actor_telegram)
    if not isinstance(approve, bool):
        raise ValidationError("Укажите решение по платежу.")
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if not payment.required or payment.status != Payment.Status.REVIEW:
        raise ValidationError("Проверять можно только платёж со статусом «На проверке».")
    reference = str(bank_reference or "").strip().upper()
    reason = str(reason or "").strip()
    if approve:
        reference = _text(reference, "Уникальный номер банковского платежа", 160)
        if reference.startswith("CQC-"):
            raise ValidationError("Укажите номер банковской операции из Kaspi, а не код участника CQC.")
        if Payment.objects.filter(bank_reference=reference).exclude(pk=payment.pk).exists():
            raise ValidationError("Этот банковский платёж уже зачтён другому участнику.")
    elif not reason:
        raise ValidationError("Укажите причину отклонения.")
    previous = payment.status
    payment.status = Payment.Status.CONFIRMED if approve else Payment.Status.REJECTED
    payment.bank_reference = reference if approve else None
    payment.reviewed_at = timezone.now()
    payment.reviewed_by = actor_user
    payment.reviewed_by_telegram = actor_telegram if actor_user is None else None
    payment.rejection_reason = "" if approve else reason
    try:
        with transaction.atomic():
            payment.save()
    except IntegrityError as exc:
        raise ValidationError("Банковский платёж уже использован. Проверьте его уникальный номер.") from exc
    PaymentAudit.objects.create(payment=payment, actor_user=actor_user, actor_telegram=payment.reviewed_by_telegram, from_status=previous, to_status=payment.status, bank_reference=payment.bank_reference or "", reason=reason)
    Outbox.objects.create(payment=payment, kind="result")
    return payment


def _roster_errors(team):
    players = list(team.participants.filter(active=True))
    errors = []
    if sum(not p.is_reserve for p in players) != team.tournament.roster_size:
        errors.append("Количество игроков основного состава не соответствует условиям.")
    if team.tournament.reserve_limit is None or sum(p.is_reserve for p in players) > team.tournament.reserve_limit:
        errors.append("Количество запасных не соответствует условиям.")
    if any(not p.registration_confirmed for p in players):
        errors.append("Не все участники подтвердили участие.")
    linked_accounts = set(TelegramIdentity.objects.filter(user_id__in=[p.account_id for p in players if p.account_id]).values_list('user_id', flat=True))
    if any(not p.account_id or p.account_id not in linked_accounts for p in players):
        errors.append("Не все участники связали заявку со своим Telegram.")
    return errors


def team_readiness(team):
    errors = _roster_errors(team)
    if not team.roster_checked_at:
        errors.append("Организатор ещё не проверил состав.")
    for participant in team.participants.filter(active=True).select_related("payment"):
        if not hasattr(participant, "payment") or (participant.payment.required and participant.payment.status != Payment.Status.CONFIRMED):
            errors.append("Не все обязательные платежи подтверждены кассиром.")
            break
    return errors


@transaction.atomic
def check_roster(team, actor_user):
    _staff(actor_user)
    Tournament.objects.select_for_update().get(pk=team.tournament_id)
    team = Team.objects.select_for_update(of=("self",)).select_related("tournament").get(pk=team.pk)
    if team.status == 'draft':
        raise ValidationError('Капитан ещё не подал состав на турнир.')
    errors = _roster_errors(team)
    if errors:
        raise ValidationError(errors)
    team.roster_checked_at = timezone.now()
    team.roster_checked_by = actor_user
    team.save(update_fields=["roster_checked_at", "roster_checked_by"])
    return team


@transaction.atomic
def confirm_team(team, actor_user):
    _staff(actor_user)
    tournament = Tournament.objects.select_for_update().get(pk=team.tournament_id)
    team = Team.objects.select_for_update(of=("self",)).select_related("tournament").get(pk=team.pk)
    if team.status == Team.Status.CONFIRMED:
        return team
    if team.status == 'draft':
        raise ValidationError('Сначала капитан должен подать команду на турнир.')
    if team.status == Team.Status.REJECTED:
        raise ValidationError("Отклонённую команду нельзя допустить без пересмотра заявки.")
    if not tournament.terms_confirmed:
        raise ValidationError("Условия турнира ещё не подтверждены.")
    if tournament.matches.exists():
        raise ValidationError("Сетка уже сформирована.")
    errors = team_readiness(team)
    if errors:
        raise ValidationError(errors)
    if tournament.teams.filter(status=Team.Status.CONFIRMED).count() >= tournament.max_teams:
        raise ValidationError("Все места в турнире заняты.")
    team.status = Team.Status.CONFIRMED
    team.save(update_fields=["status"])
    notify_team_status(team, 'Команда допущена к турниру', f'Команда {team.name} прошла проверку состава и взносов.')
    return team


@transaction.atomic
def reopen_team(team, actor_user, reason):
    """Return a rejected application to assembly without erasing any payments."""
    from django.contrib.admin.models import LogEntry, CHANGE
    _staff(actor_user)
    Tournament.objects.select_for_update().get(pk=team.tournament_id)
    team = Team.objects.select_for_update(of=('self',)).select_related('tournament').get(pk=team.pk)
    reason = _text(reason, 'Причина пересмотра', 1000)
    if team.status != Team.Status.REJECTED:
        raise ValidationError('Вернуть на сбор состава можно только отклонённую команду.')
    if team.tournament.matches.exists():
        raise ValidationError('После формирования сетки пересмотр состава недоступен.')
    previous_reason = team.rejection_reason
    team.status = Team.Status.DRAFT
    team.submitted_at = None
    team.rejection_reason = ''
    team.roster_checked_at = None
    team.roster_checked_by = None
    team.save(update_fields=['status', 'submitted_at', 'rejection_reason', 'roster_checked_at', 'roster_checked_by'])
    LogEntry.objects.log_actions(user_id=actor_user.pk, queryset=Team.objects.filter(pk=team.pk),
                                action_flag=CHANGE, change_message=f'Возврат на сбор состава: {reason}. Предыдущий отказ: {previous_reason}')
    notify_team_status(team, 'Заявка возвращена на исправление', reason)
    return team


# These entry points also remain available from arena.services for callers.
def generate_bracket(tournament, actor_user):
    from .bracket import generate_bracket as generate
    return generate(tournament, actor_user)


def record_result(match, score_a, score_b, actor_user):
    from .bracket import record_result as record
    return record(match, score_a, score_b, actor_user)
