import uuid
from datetime import date
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router, transaction
from django.utils import timezone


class Tournament(models.Model):
    title = models.CharField(max_length=160, default="CQC Tournament")
    game = models.CharField(max_length=160, default="Mobile Legends: Bang Bang")
    description = models.TextField(blank=True)
    rules = models.TextField(blank=True)
    prize_pool = models.PositiveIntegerField(null=True, blank=True, default=150000)
    prize_note = models.TextField(default="Предварительно по плану совещания; требуется подтверждение")
    event_date = models.DateField(default=date(2026, 11, 14))
    starts_at = models.DateTimeField(null=True, blank=True)
    end_registration = models.DateTimeField(null=True, blank=True)
    timezone_name = models.CharField(max_length=80, blank=True)
    max_teams = models.PositiveIntegerField(default=16)
    roster_size = models.PositiveIntegerField(default=5)
    reserve_limit = models.PositiveIntegerField(null=True, blank=True)
    reserves_pay = models.BooleanField(null=True, blank=True)
    best_of = models.PositiveIntegerField(null=True, blank=True)
    final_best_of = models.PositiveIntegerField(null=True, blank=True)
    grand_final_reset = models.BooleanField(null=True, blank=True)
    kaspi_url = models.URLField(default="https://pay.kaspi.kz/pay/xd308jri")
    registration_open = models.BooleanField(default=False)
    terms_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        errors = {}
        if self.max_teams is None or self.max_teams < 2 or self.max_teams > 64 or self.max_teams & (self.max_teams - 1):
            errors["max_teams"] = "Количество команд: 2, 4, 8, 16, 32 или 64."
        if self.roster_size is None or not 1 <= self.roster_size <= 10:
            errors["roster_size"] = "Основной состав должен содержать от 1 до 10 игроков."
        if self.reserve_limit is not None and not 0 <= self.reserve_limit <= 10:
            errors["reserve_limit"] = "Количество запасных: от 0 до 10."
        for field in ("best_of", "final_best_of"):
            if getattr(self, field) is not None and getattr(self, field) not in (1, 3, 5, 7, 9):
                errors[field] = "Поддерживаются BO1, BO3, BO5, BO7 и BO9."
        try:
            parsed_url = urlsplit(self.kaspi_url)
            if (parsed_url.scheme != "https" or parsed_url.hostname != "pay.kaspi.kz"
                    or parsed_url.username is not None or parsed_url.password is not None
                    or parsed_url.port not in (None, 443) or self.kaspi_url != self.kaspi_url.strip()):
                errors["kaspi_url"] = "Укажите HTTPS-ссылку pay.kaspi.kz без логина, пароля и стороннего порта."
        except (ValueError, TypeError):
            errors["kaspi_url"] = "Некорректная ссылка Kaspi Pay."
        zone = None
        if self.timezone_name:
            try:
                zone = ZoneInfo(self.timezone_name)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                errors["timezone_name"] = "Укажите существующий часовой пояс IANA."
        for field in ("starts_at", "end_registration"):
            if getattr(self, field) and timezone.is_naive(getattr(self, field)):
                errors[field] = "Время должно содержать часовой пояс."
        if not any(field in errors for field in ("starts_at", "end_registration")):
            if self.starts_at and self.end_registration and self.end_registration >= self.starts_at:
                errors["end_registration"] = "Регистрация должна завершаться до начала турнира."
            if self.starts_at and zone and timezone.localtime(self.starts_at, zone).date() != self.event_date:
                errors["starts_at"] = "Дата начала в часовом поясе турнира должна совпадать с датой турнира."
        if self.pk and self.participants.exists():
            previous = type(self).objects.get(pk=self.pk)
            changed = [field for field in ("roster_size", "reserve_limit", "reserves_pay")
                       if getattr(previous, field) != getattr(self, field)]
            if changed:
                payments = Payment.objects.filter(participant__tournament=self)
                financial_history = payments.filter(
                    ~models.Q(status=Payment.Status.AWAITING)
                    | (models.Q(bank_reference__isnull=False) & ~models.Q(bank_reference=""))
                    | ~models.Q(provider_payment_id="")
                    | models.Q(submitted_at__isnull=False)
                    | models.Q(reviewed_at__isnull=False)
                    | models.Q(reviewed_by__isnull=False)
                    | models.Q(reviewed_by_telegram__isnull=False)
                    | models.Q(audits__isnull=False)
                    | models.Q(notifications__isnull=False)
                    | models.Q(telegram_sessions__isnull=False)
                ).exists()
                frozen = self.participants.filter(active=True).exclude(team__status=Team.Status.DRAFT).exists() or financial_history
                for field in changed:
                    if frozen:
                        errors[field] = "Условия состава и оплаты нельзя менять после подачи команды или появления платёжной истории."
                    elif getattr(self, field) is None:
                        errors[field] = "При наличии участников нельзя возвращать условия в неопределённое состояние."
                if "reserves_pay" in changed and self.participants.filter(active=True, is_reserve=True).exists():
                    errors["reserves_pay"] = "Оплату запасных нельзя менять после добавления запасного: его взнос уже зафиксирован."
                counts = self.participants.filter(active=True).values("team_id").annotate(
                    main_count=models.Count("pk", filter=models.Q(is_reserve=False)),
                    reserve_count=models.Count("pk", filter=models.Q(is_reserve=True)),
                )
                for count in counts:
                    if self.roster_size is not None and count["main_count"] > self.roster_size:
                        errors["roster_size"] = "В черновике команды уже больше основных игроков, чем новый лимит."
                    if self.reserve_limit is not None and count["reserve_count"] > self.reserve_limit:
                        errors["reserve_limit"] = "В черновике команды уже больше запасных, чем новый лимит."
        if self.pk and self.matches.exists():
            previous = type(self).objects.get(pk=self.pk)
            if self.registration_open:
                errors["registration_open"] = "Регистрацию нельзя открыть после формирования сетки."
            for field in ("best_of", "final_best_of", "grand_final_reset", "max_teams"):
                if getattr(previous, field) != getattr(self, field):
                    errors[field] = "Параметры сетки нельзя менять после её формирования."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Registration and roster edits lock this same row. Validate only after
        # acquiring it, so a concurrent join cannot invalidate checked limits
        # between clean() and the subsequent UPDATE on PostgreSQL.
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            if self.pk:
                type(self).objects.using(using).select_for_update().filter(pk=self.pk).first()
            self.clean()
            super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class Team(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Сбор состава"
        PENDING = "pending", "Ожидает проверки"
        CONFIRMED = "confirmed", "Допущена"
        REJECTED = "rejected", "Отклонена"

    tournament = models.ForeignKey(Tournament, on_delete=models.PROTECT, related_name="teams")
    captain = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="teams")
    name = models.CharField(max_length=100)
    contact = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True)
    invite_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    roster_checked_at = models.DateTimeField(null=True, blank=True)
    roster_checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="checked_rosters")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tournament", "captain"], name="one_team_per_captain")]

    def __str__(self):
        return self.name


class Participant(models.Model):
    active = models.BooleanField(default=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='withdrawn_players')
    withdrawal_reason = models.TextField(blank=True)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="tournament_participations")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="participants")
    tournament = models.ForeignKey(Tournament, on_delete=models.PROTECT, related_name="participants", editable=False)
    nickname = models.CharField(max_length=100)
    game_id = models.CharField(max_length=40)
    server_id = models.CharField(max_length=40)
    is_reserve = models.BooleanField(default=False)
    registration_confirmed = models.BooleanField(default=False)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tournament", "game_id", "server_id"], condition=models.Q(active=True), name="unique_active_player_per_tournament")]

    def save(self, *args, **kwargs):
        self.tournament_id = self.team.tournament_id
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nickname


class Payment(models.Model):
    class Status(models.TextChoices):
        AWAITING = "awaiting", "Ожидает оплаты"
        REVIEW = "review", "На проверке"
        CONFIRMED = "confirmed", "Оплата подтверждена"
        REJECTED = "rejected", "Отклонена"

    participant = models.OneToOneField(Participant, on_delete=models.PROTECT, related_name="payment")
    required = models.BooleanField(default=True)
    amount = models.PositiveIntegerField(default=2200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AWAITING)
    bank_reference = models.CharField(max_length=160, unique=True, null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_payments")
    reviewed_by_telegram = models.PositiveBigIntegerField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    provider = models.CharField(max_length=32, default="manual_kaspi")
    provider_payment_id = models.CharField(max_length=160, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(required=True, amount=2200) | models.Q(required=False, amount=0), name="fixed_participant_fee"),
            models.CheckConstraint(condition=~models.Q(status="confirmed") | (models.Q(bank_reference__isnull=False) & ~models.Q(bank_reference="")), name="confirmed_payment_has_reference"),
        ]

    def __str__(self):
        return f"{self.participant}: {self.get_status_display()}"

    @property
    def code(self):
        return f"CQC-{self.pk:06d}"


class PaymentAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Историю проверки платежей нельзя изменять.")

    def delete(self):
        raise ValidationError("Историю проверки платежей нельзя удалять.")


class PaymentAudit(models.Model):
    objects = PaymentAuditQuerySet.as_manager()
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="audits")
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    actor_telegram = models.PositiveBigIntegerField(null=True, blank=True)
    from_status = models.CharField(max_length=16)
    to_status = models.CharField(max_length=16)
    bank_reference = models.CharField(max_length=160, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Историю проверки платежей нельзя изменять.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Историю проверки платежей нельзя удалять.")


class Match(models.Model):
    class Resolution(models.TextChoices):
        PLAYED = "played", "Сыгран"
        FORFEIT = "forfeit", "Техническое поражение"
        BYE = "bye", "Проход без игры"
        EMPTY = "empty", "Пустой узел сетки"

    class Bracket(models.TextChoices):
        UPPER = "upper", "Верхняя сетка"
        LOWER = "lower", "Нижняя сетка"
        FINAL = "final", "Гранд-финал"
        RESET = "reset", "Повторный гранд-финал"

    tournament = models.ForeignKey(Tournament, on_delete=models.PROTECT, related_name="matches")
    key = models.CharField(max_length=30)
    bracket = models.CharField(max_length=10, choices=Bracket.choices)
    round = models.PositiveIntegerField()
    index = models.PositiveIntegerField()
    team_a = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="matches_a")
    team_b = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="matches_b")
    source_a = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="feeds_a")
    source_b = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="feeds_b")
    source_a_outcome = models.CharField(max_length=6, blank=True, choices=[("winner", "Победитель"), ("loser", "Проигравший")])
    source_b_outcome = models.CharField(max_length=6, blank=True, choices=[("winner", "Победитель"), ("loser", "Проигравший")])
    scheduled_at = models.DateTimeField(null=True, blank=True)
    ready_deadline = models.DateTimeField(null=True, blank=True)
    ready_a_at = models.DateTimeField(null=True, blank=True)
    ready_b_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    lobby_details = models.TextField(blank=True, max_length=2000)
    resolution_reason = models.TextField(blank=True, max_length=1000)
    best_of = models.PositiveIntegerField()
    score_a = models.PositiveIntegerField(null=True, blank=True)
    score_b = models.PositiveIntegerField(null=True, blank=True)
    winner = models.ForeignKey(Team, on_delete=models.PROTECT, null=True, blank=True, related_name="wins")
    completed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=8, choices=Resolution.choices, blank=True, default="")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["tournament", "key"], name="unique_match_key")]

    @property
    def loser(self):
        if not self.winner_id or self.resolution in (self.Resolution.BYE, self.Resolution.EMPTY):
            return None
        return self.team_b if self.winner_id == self.team_a_id else self.team_a

    def __str__(self):
        return self.key


class MatchAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Историю матча нельзя изменять.")

    def delete(self):
        raise ValidationError("Историю матча нельзя удалять.")


class MatchAudit(models.Model):
    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="audits")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    reason = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = MatchAuditQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "pk"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Историю матча нельзя изменять.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Историю матча нельзя удалять.")


class MatchReportQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Обращение изменяется только через решение судьи.")

    def delete(self):
        raise ValidationError("Историю обращений нельзя удалять.")


class MatchReport(models.Model):
    class Kind(models.TextChoices):
        RESULT = "result", "Сообщить результат"
        DISPUTE = "dispute", "Обращение к судье"

    class Status(models.TextChoices):
        PENDING = "pending", "На рассмотрении"
        RESOLVED = "resolved", "Рассмотрено"
        REJECTED = "rejected", "Отклонено"

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="reports")
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="match_reports")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submitted_match_reports")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    message = models.TextField(max_length=2000)
    evidence_url = models.URLField(max_length=1000, blank=True)
    reported_score_a = models.PositiveIntegerField(null=True, blank=True)
    reported_score_b = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decision_reason = models.TextField(blank=True, max_length=1000)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_match_reports")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = MatchReportQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["match", "team", "kind"], condition=models.Q(status="pending"), name="one_pending_report_per_match_team_kind")]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.status != self.Status.PENDING or self.reviewed_by_id or self.reviewed_at or self.decision_reason:
                raise ValidationError("Новое обращение должно ожидать рассмотрения.")
        else:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status != self.Status.PENDING:
                raise ValidationError("Решение по обращению уже принято.")
            mutable = {"status", "decision_reason", "reviewed_by", "reviewed_at"}
            for field in self._meta.concrete_fields:
                if field.name not in mutable and getattr(previous, field.attname) != getattr(self, field.attname):
                    raise ValidationError("Сведения поданного обращения нельзя изменить.")
            if (self.status not in (self.Status.RESOLVED, self.Status.REJECTED)
                    or not self.decision_reason.strip() or not self.reviewed_by_id or not self.reviewed_at):
                raise ValidationError("Укажите решение судьи, причину и время проверки.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Историю обращений нельзя удалять.")


class Outbox(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="notifications")
    kind = models.CharField(max_length=16, choices=[("review", "Проверка"), ("result", "Результат")])
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]


class TelegramReviewSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="telegram_sessions")
    submitted_at_snapshot = models.DateTimeField()
    action = models.CharField(max_length=8, choices=[("approve", "Подтвердить"), ("reject", "Отклонить")])
    chat_id = models.BigIntegerField()
    user_id = models.BigIntegerField()
    prompt_message_id = models.BigIntegerField(null=True, blank=True)
    bank_reference = models.CharField(max_length=150, blank=True)
    reason = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class BotState(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    value = models.TextField()


class TelegramIdentity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_identity")
    telegram_id = models.PositiveBigIntegerField(unique=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    linked_at = models.DateTimeField(auto_now_add=True)


class TelegramLoginChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    nonce_hash = models.CharField(max_length=64, unique=True)
    browser_binding_hash = models.CharField(max_length=64)
    code_hash = models.CharField(max_length=64)
    purpose = models.CharField(max_length=8, choices=[("login", "Войти"), ("link", "Привязать")])
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    telegram_id = models.PositiveBigIntegerField(null=True, blank=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    prompt_message_id = models.BigIntegerField(null=True, blank=True)
    code_verified_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class GameAccount(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="game_account")
    game_id = models.CharField(max_length=40)
    server_id = models.CharField(max_length=40)
    nickname = models.CharField(max_length=100)
    lookup_provider = models.CharField(max_length=80, blank=True)
    lookup_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["game_id", "server_id"], name="unique_game_account_identity")]


class RateLimitBucket(models.Model):
    """Shared fixed-window counters; raw client addresses are never persisted."""

    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)


class GameAccountDisputeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Решение по спору изменяется только через проверку организатором.")

    def delete(self):
        raise ValidationError("Историю споров нельзя удалять.")


class GameAccountDispute(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "На рассмотрении"
        APPROVED = "approved", "Привязка исправлена"
        REJECTED = "rejected", "Отклонено"

    claimant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="game_account_disputes")
    old_owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="disputed_game_accounts")
    game_id = models.CharField(max_length=40)
    server_id = models.CharField(max_length=40)
    nickname = models.CharField(max_length=100)
    lookup_provider = models.CharField(max_length=80)
    lookup_checked_at = models.DateTimeField()
    explanation = models.TextField(max_length=2000)
    old_account_pk = models.PositiveBigIntegerField()
    old_account_updated_at = models.DateTimeField()
    claimant_account_pk = models.PositiveBigIntegerField(null=True, blank=True)
    claimant_account_updated_at = models.DateTimeField(null=True, blank=True)
    claimant_previous_game_id = models.CharField(max_length=40, blank=True)
    claimant_previous_server_id = models.CharField(max_length=40, blank=True)
    claimant_previous_nickname = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decision_reason = models.TextField(blank=True, max_length=1000)
    checked_in_game = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_game_account_disputes")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GameAccountDisputeQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["claimant", "game_id", "server_id"], condition=models.Q(status="pending"), name="one_pending_game_dispute_per_claimant")]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.status != self.Status.PENDING or self.reviewed_by_id or self.reviewed_at or self.checked_in_game or self.decision_reason:
                raise ValidationError("Новый спор должен ожидать рассмотрения.")
        else:
            previous = type(self).objects.get(pk=self.pk)
            if previous.status != self.Status.PENDING:
                raise ValidationError("Принятое решение по спору нельзя изменить.")
            mutable = {"status", "decision_reason", "checked_in_game", "reviewed_by", "reviewed_at"}
            for field in self._meta.concrete_fields:
                if field.name not in mutable and getattr(self, field.attname) != getattr(previous, field.attname):
                    raise ValidationError("Сведения поданного спора нельзя изменить.")
            if (self.status not in {self.Status.APPROVED, self.Status.REJECTED} or not self.decision_reason.strip()
                    or not self.checked_in_game or not self.reviewed_by_id or not self.reviewed_at):
                raise ValidationError("Укажите решение, причину и подтверждение проверки в игре.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Историю споров нельзя удалять.")


from .notification_models import Notification  # noqa: E402,F401
from .rule_models import RulesAcceptance  # noqa: E402,F401
