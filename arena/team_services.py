"""Self-service team assembly; joining never confirms a payment or admission."""

import re
import unicodedata

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import game_lookup
from .models import GameAccount, Participant, Payment, Team, TelegramIdentity, Tournament
from .services import registration_errors


def _identity(user):
    if (not user or not user.is_authenticated or not user.pk
            or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists()
            or not TelegramIdentity.objects.filter(user_id=user.pk).exists()):
        raise PermissionDenied("Для этого действия войдите в активный аккаунт и подключите Telegram.")


def _consent(accepted):
    if accepted is not True:
        raise ValidationError("Подтвердите согласие с правилами и участие со своим игровым аккаунтом.")


def _text(value, label, limit):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > limit
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)):
        raise ValidationError(f"{label}: укажите от 1 до {limit} символов без управляющих знаков.")
    return value.strip()


def _contacts(telegram_contact, discord_contact):
    telegram = _text(telegram_contact, "Telegram", 70)
    discord = _text(discord_contact, "Discord", 80)
    match = re.fullmatch(r"(?:@|https://t\.me/)?([A-Za-z][A-Za-z0-9_]{4,31})", telegram)
    if not match:
        raise ValidationError("Telegram: укажите @username или ссылку https://t.me/username, не телефон.")
    if ("@" in discord or re.fullmatch(r"\+?[0-9 ()-]+", discord)
            or "://" in discord):
        raise ValidationError("Discord: укажите имя пользователя, не email, телефон или ссылку.")
    return f"Telegram: @{match.group(1)}\nDiscord: {discord}"


def _locked_team(team):
    # All membership mutations lock the tournament first: counts and duplicate
    # checks remain serialized across different teams as well as within one team.
    actual_tournament_id = Team.objects.values_list("tournament_id", flat=True).get(pk=team.pk)
    tournament = Tournament.objects.select_for_update().get(pk=actual_tournament_id)
    current = Team.objects.select_for_update().get(pk=team.pk)
    current.tournament = tournament
    return current


def _captain(team, user):
    _identity(user)
    if team.captain_id != user.pk:
        raise PermissionDenied("Изменять команду может только её капитан.")


def _assembly_open(team, legacy=False):
    allowed = {Team.Status.DRAFT, Team.Status.PENDING} if legacy else {Team.Status.DRAFT}
    if team.status not in allowed or team.tournament.matches.exists():
        raise ValidationError("Состав этой команды уже закрыт для присоединения и изменений.")


def _reserve(role):
    if role not in ("main", "reserve"):
        raise ValidationError("Выберите роль: основной игрок или запасной.")
    return role == "reserve"


def _slot(team, reserve):
    limit = (team.tournament.reserve_limit or 0) if reserve else team.tournament.roster_size
    if reserve and team.tournament.reserves_pay is None:
        raise ValidationError("Условия участия запасных ещё не определены организатором.")
    if team.participants.filter(active=True, is_reserve=reserve).count() >= limit:
        raise ValidationError("Нет свободного места для выбранной роли в составе.")


def _payment(participant):
    required = not participant.is_reserve or participant.team.tournament.reserves_pay is not False
    # Existing rows keep their immutable fee snapshot, including paid rows.
    Payment.objects.get_or_create(participant=participant, defaults={"required": required, "amount": 2200 if required else 0})


def _invalidate_check(team):
    if team.roster_checked_at or team.roster_checked_by_id:
        team.roster_checked_at = None
        team.roster_checked_by = None
        team.save(update_fields=["roster_checked_at", "roster_checked_by"])


def _find_pair(tournament, game_id, server_id):
    # Legacy registration accepted digit strings with leading zeroes. Compare
    # canonical pairs without rewriting old paid participants or their tokens.
    matches = []
    for player in Participant.objects.select_for_update().filter(tournament=tournament, active=True).order_by("pk"):
        try:
            pair = game_lookup.normalize_ids(player.game_id, player.server_id)
        except ValidationError:
            continue
        if pair == (game_id, server_id):
            matches.append(player)
    if len(matches) > 1:
        raise ValidationError("Найден конфликт старых заявок этого аккаунта. Обратитесь к организатору.")
    return matches[0] if matches else None


@transaction.atomic
def create_team(tournament, captain, name, telegram_contact, discord_contact, accept_rules=True):
    _identity(captain)
    _consent(accept_rules)
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if tournament.matches.exists():
        raise ValidationError("Сетка турнира уже сформирована.")
    name = _text(name, "Название команды", 100)
    contact = _contacts(telegram_contact, discord_contact)
    if Team.objects.filter(tournament=tournament, captain_id=captain.pk).exists():
        raise ValidationError("У капитана уже есть команда в этом турнире.")
    if Participant.objects.filter(tournament=tournament, account_id=captain.pk, active=True).exists():
        raise ValidationError("Вы уже участвуете в другой команде этого турнира. Сначала выйдите из её состава.")
    if any(existing.casefold() == name.casefold() for existing in tournament.teams.values_list("name", flat=True)):
        raise ValidationError("Команда с таким названием уже существует.")
    return Team.objects.create(tournament=tournament, captain=captain, name=name, contact=contact, status=Team.Status.DRAFT)


@transaction.atomic
def join_team(team, user, role="main", accept_rules=True):
    _identity(user)
    _consent(accept_rules)
    reserve = _reserve(role)
    team = _locked_team(team)
    _assembly_open(team, legacy=True)
    if Team.objects.filter(tournament=team.tournament, captain_id=user.pk).exclude(pk=team.pk).exists():
        raise ValidationError("Вы капитан другой команды этого турнира и не можете вступить в чужой состав.")
    # Profile creation and ownership correction use the same principal lock,
    # including when a GameAccount row does not yet exist.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    profile = GameAccount.objects.select_for_update().filter(user_id=user.pk).first()
    if not profile:
        raise ValidationError("Сначала сохраните свой игровой профиль.")
    game_id, server_id = game_lookup.normalize_ids(profile.game_id, profile.server_id)
    nickname = _text(profile.nickname, "Игровой ник", 100)
    memberships = Participant.objects.filter(tournament=team.tournament, account_id=user.pk, active=True)
    player = _find_pair(team.tournament, game_id, server_id)
    if memberships.exclude(pk=player.pk if player else None).exists():
        raise ValidationError("Вы уже участвуете в другой заявке этого турнира.")
    if player:
        if player.team_id != team.pk:
            raise ValidationError("Этот игровой аккаунт уже указан в другой команде турнира.")
        if player.account_id and player.account_id != user.pk:
            raise PermissionDenied("Это место уже связано с другим участником.")
        # A reserved row determines its role. The common invite cannot promote
        # a reserve or change an existing member's role by repeating the request.
        player.team = team
        changed = player.account_id != user.pk or not player.registration_confirmed or player.nickname != nickname
        if changed:
            player.account = user
            player.registration_confirmed = True
            player.nickname = nickname
            player.save(update_fields=["account", "registration_confirmed", "nickname"])
            _invalidate_check(team)
        _payment(player)
        return player
    if team.status != Team.Status.DRAFT:
        raise ValidationError("В поданной заявке можно подтвердить только уже указанного игрока.")
    _slot(team, reserve)
    player = Participant.objects.create(team=team, account=user, nickname=nickname,
                                       game_id=game_id, server_id=server_id, is_reserve=reserve,
                                       registration_confirmed=True)
    _payment(player)
    _invalidate_check(team)
    return player


def add_player(team, captain, game_id, server_id, role="main"):
    # Authenticate and reject a closed roster before sending any ID externally.
    current = Team.objects.select_related("tournament").get(pk=team.pk)
    _captain(current, captain)
    _assembly_open(current)
    reserve = _reserve(role)
    game_id, server_id = game_lookup.normalize_ids(game_id, server_id)
    # No transaction spans HTTP. Lookup failure does not reserve a seat.
    result = game_lookup.lookup(game_id, server_id)
    if (result.game_id, result.server_id) != (game_id, server_id):
        raise game_lookup.LookupUnavailable("Сервис вернул данные другого игрового аккаунта.")
    nickname = _text(result.nickname, "Игровой ник", 100)
    with transaction.atomic():
        current = _locked_team(team)
        _captain(current, captain)
        _assembly_open(current)
        player = _find_pair(current.tournament, game_id, server_id)
        if player:
            if player.team_id != current.pk:
                raise ValidationError("Игрок уже указан в другой команде турнира.")
            # A repeated add is harmless and never changes a bound identity,
            # role, consent, nickname or an existing payment.
            player.team = current
            _payment(player)
            return player
        _slot(current, reserve)
        player = Participant.objects.create(team=current, nickname=nickname, game_id=game_id,
                                           server_id=server_id, is_reserve=reserve)
        _payment(player)
        _invalidate_check(current)
        return player


def submission_errors(team):
    """Roster readiness for organizer review; payment is checked at admission."""
    team = Team.objects.select_related("tournament").get(pk=team.pk)
    players = list(team.participants.filter(active=True).select_related("account"))
    errors = []
    if sum(not player.is_reserve for player in players) != team.tournament.roster_size:
        errors.append(f"Для подачи нужны {team.tournament.roster_size} игроков основного состава.")
    if sum(player.is_reserve for player in players) > (team.tournament.reserve_limit or 0):
        errors.append("Превышено допустимое количество запасных.")
    account_ids = [player.account_id for player in players if player.account_id]
    linked = set(TelegramIdentity.objects.filter(user_id__in=account_ids).values_list("user_id", flat=True))
    profiles = {profile.user_id: profile for profile in GameAccount.objects.filter(user_id__in=account_ids)}
    if len(account_ids) != len(set(account_ids)):
        errors.append("Один пользователь не может занимать несколько мест в составе.")
    for player in players:
        profile = profiles.get(player.account_id)
        try:
            matching_profile = profile and game_lookup.normalize_ids(profile.game_id, profile.server_id) == game_lookup.normalize_ids(player.game_id, player.server_id)
        except ValidationError:
            matching_profile = False
        if (not player.account_id or not player.account.is_active or player.account_id not in linked
                or not player.registration_confirmed or not matching_profile):
            errors.append("Все участники должны подтвердить свой игровой профиль и участие через Telegram.")
            break
    return errors


@transaction.atomic
def submit_team(team, captain):
    team = _locked_team(team)
    _captain(team, captain)
    if team.status == Team.Status.PENDING:
        return team
    _assembly_open(team)
    errors = registration_errors(team.tournament) + submission_errors(team)
    if errors:
        raise ValidationError(errors)
    team.status = Team.Status.PENDING
    team.submitted_at = timezone.now()
    team.save(update_fields=["status", "submitted_at"])
    from .services import notify_team_status
    notify_team_status(team, 'Заявка команды подана', 'Состав передан организатору. Статусы участия и взносов доступны в кабинете.')
    return team


@transaction.atomic
def remove_player(team, actor, participant_id):
    """Recover an unpaid draft seat; never remove financial history."""
    _identity(actor)
    team = _locked_team(team)
    _assembly_open(team)
    player = Participant.objects.select_for_update().filter(pk=participant_id, team=team, active=True).first()
    if not player:
        raise ValidationError("Участник не найден в этой команде.")
    if actor.pk != team.captain_id and actor.pk != player.account_id:
        raise PermissionDenied("Удалить место может капитан или сам участник.")
    payment = Payment.objects.select_for_update().filter(participant=player).first()
    if payment:
        if (payment.status != Payment.Status.AWAITING or payment.bank_reference
                or payment.provider_payment_id or payment.submitted_at or payment.reviewed_at
                or payment.reviewed_by_id or payment.reviewed_by_telegram
                or payment.audits.exists() or payment.notifications.exists()
                or payment.telegram_sessions.exists()):
            raise ValidationError("У участника есть платёж или история проверки. Обратитесь к организатору; удаление запрещено.")
        payment.delete()
    player.delete()
    _invalidate_check(team)


def _revision_open(team):
    if team.tournament.matches.exists():
        raise ValidationError('После формирования сетки изменение заявки недоступно.')
    now = timezone.now()
    if any(deadline and now >= deadline for deadline in (team.tournament.end_registration, team.tournament.starts_at)):
        raise ValidationError('Срок изменения заявки завершён. Обратитесь к организатору.')


def _journal(team, actor, message):
    from django.contrib.admin.models import CHANGE, LogEntry
    LogEntry.objects.log_actions(user_id=actor.pk, queryset=Team.objects.filter(pk=team.pk),
                                action_flag=CHANGE, change_message=message)


@transaction.atomic
def return_to_draft(team, captain, reason):
    team = _locked_team(team)
    _captain(team, captain)
    _revision_open(team)
    reason = _text(reason, 'Причина возврата', 1000)
    if team.status != Team.Status.PENDING:
        raise ValidationError('Капитан может вернуть на редактирование только заявку, ожидающую проверки.')
    team.status = Team.Status.DRAFT
    team.submitted_at = None
    team.roster_checked_at = None
    team.roster_checked_by = None
    team.save(update_fields=['status', 'submitted_at', 'roster_checked_at', 'roster_checked_by'])
    _journal(team, captain, f'Капитан отозвал заявку на редактирование: {reason}. Платежи сохранены.')
    from .services import notify_team_status
    notify_team_status(team, 'Капитан вернул заявку на редактирование', reason)
    return team


@transaction.atomic
def update_team_details(team, captain, name, telegram_contact, discord_contact):
    team = _locked_team(team)
    _captain(team, captain)
    _assembly_open(team)
    _revision_open(team)
    name = _text(name, 'Название команды', 100)
    contact = _contacts(telegram_contact, discord_contact)
    if any(existing.casefold() == name.casefold() for existing in team.tournament.teams.exclude(pk=team.pk).values_list('name', flat=True)):
        raise ValidationError('Команда с таким названием уже существует.')
    previous = team.name
    team.name, team.contact = name, contact
    team.save(update_fields=['name', 'contact'])
    _invalidate_check(team)
    _journal(team, captain, f'Изменены название и контакты команды: {previous} → {name}.')
    return team


@transaction.atomic
def rotate_invite(team, captain):
    import uuid
    team = _locked_team(team)
    _captain(team, captain)
    _assembly_open(team)
    _revision_open(team)
    team.invite_token = uuid.uuid4()
    team.save(update_fields=['invite_token'])
    _journal(team, captain, 'Капитан обновил общую ссылку приглашения. Прежняя ссылка отключена.')
    return team


@transaction.atomic
def change_player_role(team, captain, participant_id, role):
    team = _locked_team(team)
    _captain(team, captain)
    _assembly_open(team)
    _revision_open(team)
    reserve = _reserve(role)
    player = Participant.objects.select_for_update().filter(pk=participant_id, team=team, active=True).first()
    if not player:
        raise ValidationError('Участник не найден в этой команде.')
    payment = Payment.objects.select_for_update().filter(participant=player).first()
    if not payment or (payment.status != Payment.Status.AWAITING or payment.bank_reference
            or payment.provider_payment_id or payment.submitted_at or payment.reviewed_at
            or payment.reviewed_by_id or payment.reviewed_by_telegram or payment.rejection_reason
            or payment.audits.exists() or payment.notifications.exists() or payment.telegram_sessions.exists()):
        raise ValidationError('Роль нельзя менять после отправки взноса или появления истории проверки.')
    if player.is_reserve == reserve:
        return player
    _slot(team, reserve)
    player.is_reserve = reserve
    player.save(update_fields=['is_reserve'])
    payment.required = not reserve or team.tournament.reserves_pay is not False
    payment.amount = 2200 if payment.required else 0
    payment.save(update_fields=['required', 'amount'])
    _invalidate_check(team)
    _journal(team, captain, f'Изменена роль участника {player.pk}: {role}; взнос до подачи обновлён до {payment.amount}.')
    return player
