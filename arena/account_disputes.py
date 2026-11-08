"""Private, reviewed recovery for unused MLBB profiles, never tournament money."""
import unicodedata

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import game_lookup
from .models import GameAccount, GameAccountDispute, Participant, TelegramIdentity


def _identity(user):
    if (not user or not user.is_authenticated
            or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists()
            or not TelegramIdentity.objects.filter(user_id=user.pk).exists()):
        raise PermissionDenied("Для обращения войдите через Telegram.")


def _staff(user):
    if (not user or not user.is_authenticated
            or not get_user_model().objects.filter(pk=user.pk, is_active=True, is_staff=True).exists()):
        raise PermissionDenied("Рассматривать обращения может только организатор.")


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValidationError(f"{label}: укажите от 1 до {maximum} символов.")
    # Paragraph breaks are useful for an explanation, other control characters
    # cannot meaningfully document an account dispute.
    if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\r\n\t" for char in value):
        raise ValidationError(f"{label}: недопустимые управляющие символы.")
    return value.strip()


def submit_dispute(user, game_id, server_id, explanation):
    _identity(user)
    game_id, server_id = game_lookup.normalize_ids(game_id, server_id)
    explanation = _text(explanation, "Обоснование", 2000)
    pending = GameAccountDispute.objects.filter(claimant_id=user.pk, game_id=game_id, server_id=server_id, status=GameAccountDispute.Status.PENDING)
    if pending.exists():
        raise ValidationError("Обращение по этой паре уже рассматривается. Следите за ответом ниже.")
    target = GameAccount.objects.filter(game_id=game_id, server_id=server_id).first()
    if not target or target.user_id == user.pk:
        raise ValidationError("Конфликтующей привязки для этой пары нет. Проверьте свой игровой профиль.")
    # No write transaction surrounds provider HTTP. The response is a nickname
    # lookup only; it never constitutes proof of game-account ownership.
    result = game_lookup.lookup(game_id, server_id)
    if (result.game_id, result.server_id) != (game_id, server_id):
        raise ValidationError("Поиск вернул другую пару ID и сервера. Обращение не создано.")
    nickname = _text(result.nickname, "Никнейм из поиска", 100)
    provider = _text(result.provider, "Сервис поиска", 80)
    checked_at = timezone.now()
    try:
        with transaction.atomic():
            owner_id = target.user_id
            # Match profile recovery/join locking: principals first, followed
            # by profile rows in a stable order (PostgreSQL deadlock avoidance).
            list(get_user_model().objects.select_for_update().filter(
                pk__in=[owner_id, user.pk]).order_by("pk"))
            _identity(user)
            accounts = list(GameAccount.objects.select_for_update().filter(
                user_id__in=[owner_id, user.pk]).order_by("pk"))
            target = next((account for account in accounts if (account.game_id, account.server_id) == (game_id, server_id)), None)
            if not target or target.user_id != owner_id:
                raise ValidationError("Привязка изменилась. Проверьте игровой профиль и начните обращение заново.")
            current = next((account for account in accounts if account.user_id == user.pk), None)
            dispute = GameAccountDispute(
                claimant_id=user.pk, old_owner_id=target.user_id,
                game_id=game_id, server_id=server_id, nickname=nickname,
                lookup_provider=provider, lookup_checked_at=checked_at, explanation=explanation,
                old_account_pk=target.pk, old_account_updated_at=target.updated_at,
                claimant_account_pk=current.pk if current else None,
                claimant_account_updated_at=current.updated_at if current else None,
                claimant_previous_game_id=current.game_id if current else "",
                claimant_previous_server_id=current.server_id if current else "",
                claimant_previous_nickname=current.nickname if current else "",
            )
            dispute.full_clean()
            dispute.save()
            return dispute
    except IntegrityError:
        raise ValidationError("Обращение по этой паре уже создано. Обновите страницу.") from None


def approval_blockers(dispute):
    """Display hints only; the decision service repeats all checks under locks."""
    blockers = []
    if Participant.objects.filter(account_id=dispute.old_owner_id, active=True).exists():
        blockers.append("У прежнего пользователя есть активные места в составе. Сначала отдельно разберите и архивируйте их с сохранением финансовой истории; это решение не выводит игрока из команды.")
    if Participant.objects.filter(account_id=dispute.claimant_id, active=True).exists():
        blockers.append("У заявителя есть активные места в составе. До отдельного разбора этих участий замена профиля запрещена.")
    return blockers


@transaction.atomic
def review_dispute(dispute_id, approve, reason, checked_in_game, actor):
    _staff(actor)
    if not isinstance(approve, bool):
        raise ValidationError("Выберите решение по обращению.")
    if checked_in_game is not True:
        raise ValidationError("Подтвердите проверку ситуации в игре.")
    reason = _text(reason, "Причина решения", 1000)
    dispute = GameAccountDispute.objects.select_for_update().get(pk=dispute_id)
    if dispute.status != GameAccountDispute.Status.PENDING:
        raise ValidationError("Решение по этому обращению уже принято и не может быть изменено.")
    if approve:
        # Lock both profile principals and their current rows in a stable order.
        users = {user.pk: user for user in get_user_model().objects.select_for_update().filter(
            pk__in=[dispute.old_owner_id, dispute.claimant_id]).order_by("pk")}
        claimant = users[dispute.claimant_id]
        _identity(claimant)
        accounts = {account.user_id: account for account in GameAccount.objects.select_for_update().filter(
            user_id__in=[dispute.old_owner_id, dispute.claimant_id]).order_by("pk")}
        target = accounts.get(dispute.old_owner_id)
        current = accounts.get(dispute.claimant_id)
        if (not target or target.pk != dispute.old_account_pk
                or target.updated_at != dispute.old_account_updated_at
                or (target.game_id, target.server_id) != (dispute.game_id, dispute.server_id)):
            raise ValidationError("Оспариваемая привязка изменилась после обращения. Перенос запрещён; рассмотрите ситуацию заново.")
        if ((current.pk if current else None) != dispute.claimant_account_pk
                or (current.updated_at if current else None) != dispute.claimant_account_updated_at):
            raise ValidationError("Профиль заявителя изменился после обращения. Перенос запрещён; нужно новое обращение.")
        blockers = approval_blockers(dispute)
        if blockers:
            raise ValidationError(blockers)
        try:
            with transaction.atomic():
                target.delete()
                if current is None:
                    current = GameAccount(user=claimant)
                current.game_id, current.server_id = dispute.game_id, dispute.server_id
                current.nickname = dispute.nickname
                current.lookup_provider = dispute.lookup_provider
                current.lookup_checked_at = dispute.lookup_checked_at
                current.full_clean()
                current.save()
        except IntegrityError:
            raise ValidationError("Привязки изменились во время проверки. Ничего не перенесено; обновите страницу.") from None
    dispute.status = GameAccountDispute.Status.APPROVED if approve else GameAccountDispute.Status.REJECTED
    dispute.decision_reason = reason
    dispute.checked_in_game = True
    dispute.reviewed_by = actor
    dispute.reviewed_at = timezone.now()
    dispute.full_clean()
    dispute.save(update_fields=["status", "decision_reason", "checked_in_game", "reviewed_by", "reviewed_at"])
    return dispute


class DisputeForm(forms.Form):
    game_id = forms.RegexField(label="Game ID", regex=r"^[0-9]{1,40}$", max_length=40, widget=forms.TextInput(attrs={"inputmode": "numeric"}))
    server_id = forms.RegexField(label="Server ID", regex=r"^[0-9]{1,40}$", max_length=40, widget=forms.TextInput(attrs={"inputmode": "numeric"}))
    explanation = forms.CharField(label="Почему нужна проверка", max_length=2000, widget=forms.Textarea(attrs={"rows": 4}), help_text="Опишите ситуацию и способ проверки в игре. Не отправляйте пароли, коды входа, банковские данные или документы.")

    def clean(self):
        data = super().clean()
        if data.get("game_id") and data.get("server_id"):
            data["game_id"], data["server_id"] = game_lookup.normalize_ids(data["game_id"], data["server_id"])
        return data


class DisputeReviewForm(forms.Form):
    decision = forms.ChoiceField(label="Решение", choices=[("reject", "Отклонить обращение"), ("approve", "Исправить привязку профиля")])
    reason = forms.CharField(label="Ответ заявителю", max_length=1000, widget=forms.Textarea(attrs={"rows": 4}), help_text="Объясните решение без имени, Telegram ID и других персональных данных прежнего пользователя.")
    checked_in_game = forms.BooleanField(label="Я проверил ситуацию в игре и сверил Game ID и Server ID. Поиск ника сам по себе не доказывает владение.")


def _private_response(response):
    response["Cache-Control"] = "no-store, private"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@never_cache
@require_http_methods(["GET", "POST"])
@login_required
def create_dispute(request):
    _identity(request.user)
    form = DisputeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            submit_dispute(request.user, **form.cleaned_data)
            messages.success(request, "Обращение отправлено организатору. Привязка ещё не изменена.")
            return redirect("account_disputes")
        except (ValidationError, game_lookup.LookupUnavailable, game_lookup.LookupNotFound) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else str(exc))
    # No target-owner object or metadata is included in the public projection.
    own = GameAccountDispute.objects.filter(claimant=request.user)
    rows = [{"id": item.pk, "game_id": item.game_id, "server_id": item.server_id, "nickname": item.nickname,
             "status": item.status, "status_label": item.get_status_display(), "created_at": item.created_at,
             "reviewed_at": item.reviewed_at, "decision_reason": item.decision_reason} for item in own[:30]]
    return _private_response(render(request, "arena/account_disputes.html", {"form": form, "disputes": rows}))


@never_cache
@require_GET
@login_required
def dispute_queue(request):
    _staff(request.user)
    pending = GameAccountDispute.objects.filter(status=GameAccountDispute.Status.PENDING).select_related("claimant", "old_owner").order_by("created_at", "pk")
    reviewed = GameAccountDispute.objects.exclude(status=GameAccountDispute.Status.PENDING).select_related("claimant", "reviewed_by")[:30]
    return _private_response(render(request, "arena/account_disputes_queue.html", {"pending_disputes": pending, "reviewed_disputes": reviewed}))


@never_cache
@require_http_methods(["GET", "POST"])
@login_required
def review_dispute_view(request, dispute_id):
    _staff(request.user)
    dispute = get_object_or_404(GameAccountDispute.objects.select_related("claimant", "old_owner", "reviewed_by"), pk=dispute_id)
    form = DisputeReviewForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            review_dispute(dispute.pk, form.cleaned_data["decision"] == "approve", form.cleaned_data["reason"], form.cleaned_data["checked_in_game"], request.user)
            messages.success(request, "Решение сохранено. История участия и платежи не изменены.")
            return redirect("account_dispute_review", dispute_id=dispute.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return _private_response(render(request, "arena/account_disputes_review.html", {
        "dispute": dispute, "form": form, "approval_blockers": approval_blockers(dispute),
    }))
