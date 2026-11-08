"""Session-bound Telegram device login. Identity is accepted only from bot updates.

The deep link alone cannot authorize login: a separate browser-only code, a bot
confirmation and an explicit CSRF-protected browser confirmation are required.
"""

import hashlib
import re
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import BotState, GameAccount, Participant, Team, TelegramIdentity, TelegramLoginChallenge

SESSION_KEY = "telegram_auth_challenge"
EXPIRED = "Запрос входа недоступен или истёк. Начните вход заново на сайте."
PRIVATE = "Подтверждение входа доступно только в личном чате с ботом."


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_hash(challenge_id, code):
    return salted_hmac("arena.telegram.browser_code", f"{challenge_id}:{code}", algorithm="sha256").hexdigest()


def auth_available():
    return bool(getattr(settings, "TELEGRAM_BOT_TOKEN", "") and re.fullmatch(r"[A-Za-z0-9_]{5,32}", getattr(settings, "TELEGRAM_BOT_USERNAME", "")))


def _valid_return_token(value):
    try:
        token = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(token) if Participant.objects.filter(token=token).exists() else None


def _valid_team_token(value):
    try:
        token = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(token) if Team.objects.filter(invite_token=token).exists() else None


def _bound_challenge(request, lock=False):
    saved = request.session.get(SESSION_KEY, {})
    if not isinstance(saved, dict) or not saved.get("binding"):
        raise ValidationError(EXPIRED)
    try:
        challenge_id = uuid.UUID(str(saved.get("id")))
    except (ValueError, TypeError):
        raise ValidationError(EXPIRED) from None
    query = TelegramLoginChallenge.objects.select_for_update() if lock else TelegramLoginChallenge.objects
    challenge = query.filter(pk=challenge_id).first()
    if not challenge or not constant_time_compare(challenge.browser_binding_hash, _digest(saved["binding"])):
        raise ValidationError(EXPIRED)
    if challenge.purpose == "link":
        if not request.user.is_authenticated or not request.user.is_active or request.user.pk != challenge.target_user_id:
            raise ValidationError("Аккаунт в браузере изменился. Начните привязку заново.")
    elif request.user.is_authenticated and not challenge.consumed_at:
        raise ValidationError("Вы уже вошли в аккаунт. Для привязки используйте отдельную кнопку в профиле.")
    return challenge


def _active(challenge):
    if challenge.cancelled_at or challenge.consumed_at or challenge.expires_at <= timezone.now():
        raise ValidationError(EXPIRED)


def begin_auth(request, purpose="login", participant_token=None, team_token=None):
    if not auth_available():
        raise ValidationError("Вход через Telegram пока не настроен.")
    if purpose not in {"login", "link"}:
        raise ValidationError("Неизвестный способ входа.")
    if purpose == "link" and (not request.user.is_authenticated or not request.user.is_active):
        raise ValidationError("Сначала войдите в существующий аккаунт для привязки Telegram.")
    if purpose == "login" and request.user.is_authenticated:
        raise ValidationError("Для текущего аккаунта используйте привязку Telegram.")
    return_token = _valid_return_token(participant_token or request.session.get("participant_return_token"))
    if participant_token and not return_token:
        raise ValidationError("Личная ссылка участника недействительна.")
    return_team = _valid_team_token(team_token or request.session.get('team_return_token'))
    if team_token and not return_team:
        raise ValidationError('Приглашение в команду недействительно.')
    nonce, binding = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1000000):06d}"
    challenge_id = uuid.uuid4()
    with transaction.atomic():
        try:
            previous = _bound_challenge(request, lock=True)
        except ValidationError:
            previous = None
        if previous and not previous.consumed_at and not previous.cancelled_at:
            previous.cancelled_at = timezone.now()
            previous.save(update_fields=["cancelled_at"])
        challenge = TelegramLoginChallenge.objects.create(
            id=challenge_id, nonce_hash=_digest(nonce), browser_binding_hash=_digest(binding),
            code_hash=_code_hash(challenge_id, code), purpose=purpose,
            target_user=request.user if purpose == "link" else None,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
    # This application uses Django's server-side DB sessions. Neither the
    # browser code nor binding secret is placed in a URL or Telegram payload.
    request.session[SESSION_KEY] = {"id": str(challenge.pk), "binding": binding, "nonce": nonce, "code": code}
    if return_team and not participant_token:
        request.session['team_return_token'] = return_team
        request.session.pop('participant_return_token', None)
    elif return_token:
        request.session["participant_return_token"] = return_token
        request.session.pop('team_return_token', None)
    return challenge


def auth_state(request):
    try:
        challenge = _bound_challenge(request)
    except ValidationError:
        return {"state": "none", "purpose": "link" if request.user.is_authenticated else "login", "telegram": None}
    state = "pending"
    if challenge.consumed_at:
        state = "consumed"
    elif challenge.cancelled_at:
        state = "cancelled"
    elif challenge.expires_at <= timezone.now():
        state = "expired"
    elif challenge.approved_at:
        state = "ready"
    saved = request.session[SESSION_KEY]
    active = state in {"pending", "ready"}
    return {
        "state": state, "purpose": challenge.purpose,
        "progress": ('code_verified' if challenge.code_verified_at else 'bot_opened' if challenge.prompt_message_id else 'waiting') if state == 'pending' else None,
        "browser_code": saved.get("code", "") if active else "",
        "deep_link": f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start=login_{saved['nonce']}" if active else "",
        "expires_at": challenge.expires_at.isoformat(),
        "telegram": {"id": challenge.telegram_id, "username": challenge.username, "first_name": challenge.first_name, "last_name": challenge.last_name} if state == "ready" else None,
    }


@transaction.atomic
def complete_auth(request, expected_telegram_id):
    challenge = _bound_challenge(request, lock=True)
    _active(challenge)
    if not challenge.code_verified_at or not challenge.approved_at or not challenge.telegram_id:
        raise ValidationError("Сначала введите код и подтвердите вход в Telegram.")
    if str(expected_telegram_id) != str(challenge.telegram_id):
        raise ValidationError("Подтвердите именно тот Telegram-аккаунт, который показан на странице.")
    if challenge.purpose == "link":
        user = get_user_model().objects.select_for_update().get(pk=challenge.target_user_id)
        if not user.is_active:
            raise ValidationError("Аккаунт недоступен.")
    identity = TelegramIdentity.objects.select_for_update(of=("self",)).select_related("user").filter(telegram_id=challenge.telegram_id).first()
    if challenge.purpose == "link":
        current = TelegramIdentity.objects.select_for_update().filter(user=user).first()
        if (identity and identity.user_id != user.pk) or (current and current.telegram_id != challenge.telegram_id):
            raise ValidationError("Telegram уже связан с другим аккаунтом. Автоматическое объединение запрещено.")
    elif identity:
        user = identity.user
        if not user.is_active:
            raise ValidationError("Аккаунт недоступен.")
    else:
        # Never match an existing account by username or promote the cashier ID.
        user = get_user_model().objects.create_user(
            username=f"telegram_{uuid.uuid4().hex}", password=None,
            first_name=challenge.first_name, last_name=challenge.last_name,
            is_staff=False, is_superuser=False,
        )
    values = {key: getattr(challenge, key) for key in ("username", "first_name", "last_name")}
    try:
        with transaction.atomic():
            if identity:
                for key, value in values.items():
                    setattr(identity, key, value)
                identity.save(update_fields=list(values))
            else:
                TelegramIdentity.objects.create(user=user, telegram_id=challenge.telegram_id, **values)
    except IntegrityError:
        raise ValidationError("Telegram уже связан с аккаунтом. Начните вход заново.") from None
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at"])
    return user


def _private_user(sender, chat):
    identity = sender.get("id")
    return type(identity) is int and 0 < identity < 2**63 and not sender.get("is_bot", False) and chat.get("type") == "private" and type(chat.get("id")) is int and chat["id"] == identity


def _bot_challenge(challenge_id, sender, chat):
    if not _private_user(sender, chat):
        raise ValidationError(PRIVATE)
    try:
        challenge_id = uuid.UUID(str(challenge_id))
    except (ValueError, TypeError):
        raise ValidationError(EXPIRED) from None
    challenge = TelegramLoginChallenge.objects.select_for_update().filter(pk=challenge_id, telegram_id=sender["id"]).first()
    if not challenge:
        raise ValidationError(EXPIRED)
    _active(challenge)
    return challenge


def _confirmation(bot, challenge):
    action = "привязку Telegram к существующему аккаунту" if challenge.purpose == "link" else "вход в аккаунт турнира"
    bot._send(
        f"Код из браузера принят. Подтвердите {action}, только если вы сами начали это действие "
        "на открытой странице сайта. Если ссылку прислал другой человек, отмените запрос. "
        "После подтверждения вернитесь в тот же браузер и проверьте свой Telegram-аккаунт.",
        chat_id=challenge.telegram_id,
        reply_markup={"inline_keyboard": [[
            {"text": "Подтверждаю", "callback_data": f"ta:y:{challenge.pk.hex}"},
            {"text": "Отмена", "callback_data": f"ta:n:{challenge.pk.hex}"},
        ]]},
    )


def handle_auth_update(bot, update):
    """Called only for updates read through the authenticated Telegram Bot API."""
    query = update.get("callback_query")
    if query and str(query.get("data", "")).startswith("ta:"):
        parts = query["data"].split(":")
        try:
            if len(parts) != 3 or parts[1] not in {"y", "n"}:
                raise ValidationError(EXPIRED)
            with transaction.atomic():
                challenge = _bot_challenge(parts[2], query.get("from", {}), query.get("message", {}).get("chat", {}))
                if parts[1] == "n":
                    challenge.cancelled_at = timezone.now()
                    challenge.save(update_fields=["cancelled_at"])
                    answer = "Запрос отменён."
                else:
                    if not challenge.code_verified_at:
                        raise ValidationError("Сначала введите код с открытой страницы сайта.")
                    if not challenge.approved_at:
                        challenge.approved_at = timezone.now()
                        challenge.save(update_fields=["approved_at"])
                    answer = "Подтверждено. Вернитесь в исходный браузер и завершите вход."
        except ValidationError as exc:
            answer = " ".join(exc.messages)
        bot._answer(query, answer)
        return True
    message = update.get("message", {})
    sender, chat = message.get("from", {}), message.get("chat", {})
    if not _private_user(sender, chat):
        return False
    text = str(message.get("text", ""))
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].split("@", 1)[0] == "/start" and parts[1].startswith("login_"):
        nonce = parts[1][6:]
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{32}", nonce):
                raise ValidationError(EXPIRED)
            with transaction.atomic():
                challenge = TelegramLoginChallenge.objects.select_for_update().filter(nonce_hash=_digest(nonce)).first()
                if not challenge:
                    raise ValidationError(EXPIRED)
                _active(challenge)
                if challenge.telegram_id and challenge.telegram_id != sender["id"]:
                    raise ValidationError(EXPIRED)
                challenge.telegram_id = sender["id"]
                for key, maximum in (("username", 64), ("first_name", 150), ("last_name", 150)):
                    setattr(challenge, key, str(sender.get(key, ""))[:maximum])
                challenge.save(update_fields=["telegram_id", "username", "first_name", "last_name"])
            if challenge.code_verified_at:
                _confirmation(bot, challenge)
            else:
                response = bot._send(
                    "Вход на сайт турнира. Ответьте именно на это сообщение шестизначным кодом, "
                    "показанным в вашем браузере на странице входа. В ссылке этого кода нет. "
                    "Не вводите коды, полученные от другого человека. Если вы не начинали вход, проигнорируйте запрос.",
                    chat_id=sender["id"], reply_markup={"force_reply": True, "selective": True},
                )
                with transaction.atomic():
                    current = _bot_challenge(challenge.pk, sender, chat)
                    current.prompt_message_id = response["message_id"]
                    current.save(update_fields=["prompt_message_id"])
        except ValidationError as exc:
            bot._send(" ".join(exc.messages), chat_id=sender["id"])
        return True
    prompt_id = message.get("reply_to_message", {}).get("message_id")
    challenge = TelegramLoginChallenge.objects.filter(telegram_id=sender["id"], prompt_message_id=prompt_id).order_by("-created_at").first() if type(prompt_id) is int else None
    if not challenge:
        return False
    try:
        error = None
        with transaction.atomic():
            challenge = _bot_challenge(challenge.pk, sender, chat)
            if not re.fullmatch(r"[0-9]{6}", text.strip()) or not constant_time_compare(challenge.code_hash, _code_hash(challenge.pk, text.strip())):
                # A failed denial-message delivery can replay the same update.
                # Count the user's answer once, even across webhook restarts.
                update_id = update.get("update_id")
                fresh_attempt = True
                if type(update_id) is int:
                    _, fresh_attempt = BotState.objects.get_or_create(
                        key=f"telegram_auth_attempt:{challenge.pk.hex}:{update_id}",
                        defaults={"value": "counted"},
                    )
                if fresh_attempt:
                    challenge.attempts += 1
                    if challenge.attempts >= 5:
                        challenge.cancelled_at = timezone.now()
                    challenge.save(update_fields=["attempts", "cancelled_at"])
                error = "Код не подходит. Проверьте код в исходном браузере." if not challenge.cancelled_at else EXPIRED
            elif not challenge.code_verified_at:
                challenge.code_verified_at = timezone.now()
                challenge.save(update_fields=["code_verified_at"])
        if error:
            bot._send(error, chat_id=sender["id"])
        else:
            _confirmation(bot, challenge)
    except ValidationError as exc:
        bot._send(" ".join(exc.messages), chat_id=sender["id"])
    return True


def _page(request, error=None, status=200):
    state = auth_state(request)
    if state.get("expires_at"):
        state["expires_at"] = parse_datetime(state["expires_at"])
    response = render(request, "arena/telegram_auth.html", {"auth": state, "auth_error": error}, status=status)
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Cache-Control"] = "no-store, private"
    return response


@never_cache
@require_GET
def auth_page(request):
    return _page(request)


@never_cache
@require_POST
def auth_begin(request):
    try:
        begin_auth(request, request.POST.get("purpose", "login"), request.POST.get("participant_token"), request.POST.get('team_token'))
    except ValidationError as exc:
        return _page(request, " ".join(exc.messages), 400)
    return redirect("telegram_auth")


@never_cache
@require_GET
def auth_status(request):
    response = JsonResponse(auth_state(request))
    response["Cache-Control"] = "no-store, private"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@never_cache
@require_POST
def auth_finish(request):
    try:
        if request.POST.get("confirmed") != "on":
            raise ValidationError("Подтвердите, что показанный Telegram-аккаунт принадлежит вам.")
        user = complete_auth(request, request.POST.get("telegram_id", ""))
    except ValidationError as exc:
        return _page(request, " ".join(exc.messages), 400)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Telegram подтверждён. Вы вошли в аккаунт.")
    token = _valid_return_token(request.session.get("participant_return_token"))
    team_token = _valid_team_token(request.session.get('team_return_token'))
    if not GameAccount.objects.filter(user=user).exists():
        return redirect("game_account")
    if team_token:
        return redirect('team_join', invite_token=team_token)
    if token:
        return redirect("participant", token=token)
    return redirect("captain")


@never_cache
@require_POST
def auth_cancel(request):
    try:
        with transaction.atomic():
            challenge = _bound_challenge(request, lock=True)
            if not challenge.consumed_at:
                challenge.cancelled_at = timezone.now()
                challenge.save(update_fields=["cancelled_at"])
    except ValidationError:
        pass
    return redirect("telegram_auth")
