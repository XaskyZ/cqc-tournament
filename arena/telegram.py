"""Private cashier workflow; Telegram delivery is deliberately at least once."""

import json
import logging
import uuid
from datetime import timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import BotState, Outbox, Payment, TelegramReviewSession
from .services import review_payment

logger = logging.getLogger(__name__)
DENIED = "Доступ закрыт. Проверка доступна только назначенному кассиру в личном чате."
STALE = "Действие недоступно или устарело. Откройте актуальную карточку через /pending."


class TelegramError(Exception):
    """Contains only a method and safe status, never an HTTP URL or response."""

    def __init__(self, message, *, method=None, status=None):
        super().__init__(message)
        self.method = method
        self.status = status

    @property
    def permanent_delivery_failure(self):
        return self.method == "sendMessage" and self.status in (400, 403)


class TelegramClient:
    METHODS = {"getUpdates", "getWebhookInfo", "sendMessage", "answerCallbackQuery"}

    def __init__(self, token=None):
        self._token = token or settings.TELEGRAM_BOT_TOKEN
        if not self._token:
            raise TelegramError("Telegram token is not configured")

    def call(self, method, **payload):
        if method not in self.METHODS:
            raise TelegramError("Unsupported Telegram method")
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise TelegramError(f"{method}: HTTP {exc.code}", method=method, status=exc.code) from None
        except (URLError, TimeoutError, OSError, HTTPException, ValueError):
            raise TelegramError(f"{method}: transport failure", method=method) from None
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("error_code") if isinstance(result, dict) else None
            status = status if type(status) is int else None
            raise TelegramError(f"{method}: API failure", method=method, status=status)
        return result.get("result")


class CashierBot:
    def __init__(self, client=None):
        self.client = client or TelegramClient()
        self.cashier_id = int(getattr(settings, "TELEGRAM_CASHIER_ID", 0) or 0)

    def _authorized(self, sender, chat):
        return (
            self.cashier_id > 0
            and type(sender.get("id")) is int
            and sender["id"] == self.cashier_id
            and not sender.get("is_bot", False)
            and chat.get("type") == "private"
            and type(chat.get("id")) is int
            and chat["id"] == self.cashier_id
        )

    def _send(self, text, chat_id=None, **kwargs):
        return self.client.call("sendMessage", chat_id=chat_id or self.cashier_id, text=text, **kwargs)

    def _answer(self, query, text):
        # A callback may have expired while the process was offline. Its answer
        # is cosmetic and must not prevent recording the processed update offset.
        try:
            self.client.call("answerCallbackQuery", callback_query_id=query["id"], text=text[:200])
        except TelegramError:
            logger.warning("answerCallbackQuery failed")

    @staticmethod
    def _summary(payment):
        participant = payment.participant
        submitted = timezone.localtime(payment.submitted_at).strftime("%d.%m.%Y %H:%M %Z") if payment.submitted_at else "—"
        summary = (
            f"Код: CQC-{payment.pk:06d}\nКоманда: {' '.join(participant.team.name.split())}\n"
            f"Статус команды: {participant.team.get_status_display()}\n"
            f"Участник: {' '.join(participant.nickname.split())}\nСумма: {payment.amount} ₸\nЗаявлено: {submitted}"
        )
        if participant.team.status == "rejected":
            summary += (
                "\n\nКоманда отклонена. Подтверждение фактического поступления не даёт допуск к турниру. "
                "Возврат выполняется отдельно вручную в Kaspi."
            )
        if not participant.active:
            summary += (
                "\n\nУчастник выведен из состава. Сверяем только ранее заявленное поступление; "
                "подтверждение не восстанавливает участие. Новая заявка и её взнос оформляются отдельно. "
                "Возврат выполняется отдельно вручную в Kaspi."
            )
        return summary

    def send_review_card(self, payment):
        if not self.cashier_id:
            raise TelegramError("Cashier is not configured")
        if payment.status != Payment.Status.REVIEW or not payment.submitted_at:
            self._send("Заявка уже обработана.\n" + self._summary(payment))
            return
        sessions = []
        for action in ("approve", "reject"):
            sessions.append(TelegramReviewSession.objects.create(
                payment=payment, submitted_at_snapshot=payment.submitted_at, action=action,
                chat_id=self.cashier_id, user_id=self.cashier_id,
                expires_at=timezone.now() + timedelta(hours=24),
            ))
        try:
            self._send(
                "Требуется ручная сверка поступления в Kaspi. Заявление участника не подтверждает оплату.\n\n"
                + self._summary(payment),
                reply_markup={"inline_keyboard": [[
                    {"text": "Подтвердить", "callback_data": f"pr:a:{sessions[0].pk.hex}"},
                    {"text": "Отклонить", "callback_data": f"pr:r:{sessions[1].pk.hex}"},
                ]]},
            )
        except TelegramError:
            # Failed delivery must not create an unbounded stream of sessions.
            # If Telegram accepted a card before a timeout, its old buttons are
            # safely stale; the pending outbox will deliver a fresh card.
            TelegramReviewSession.objects.filter(
                pk__in=[session.pk for session in sessions], used_at__isnull=True,
                prompt_message_id__isnull=True,
            ).delete()
            raise

    def flush_outbox(self, limit=20):
        if settings.DEMO_MODE or not self.cashier_id:
            return
        TelegramReviewSession.objects.filter(expires_at__lte=timezone.now(), used_at__isnull=True).delete()
        for item in Outbox.objects.filter(sent_at__isnull=True).select_related("payment__participant__team")[:limit]:
            Outbox.objects.filter(pk=item.pk).update(attempts=F("attempts") + 1)
            try:
                if item.kind == "review":
                    self.send_review_card(item.payment)
                else:
                    self._send(f"Текущий статус: {item.payment.get_status_display()}\n" + self._summary(item.payment))
            except TelegramError:
                # Exception messages/URLs and financial references never go into logs.
                Outbox.objects.filter(pk=item.pk).update(last_error="Telegram delivery failed; retry pending")
                logger.warning("sendMessage delivery failed")
            else:
                Outbox.objects.filter(pk=item.pk).update(sent_at=timezone.now(), last_error="")

    def _session(self, session_id, sender, chat):
        if not self._authorized(sender, chat):
            raise PermissionDenied
        try:
            session_id = uuid.UUID(session_id)
        except (ValueError, TypeError, AttributeError):
            raise ValidationError(STALE) from None
        session = TelegramReviewSession.objects.select_for_update().filter(pk=session_id).first()
        if not session or session.chat_id != chat["id"] or session.user_id != sender["id"]:
            raise ValidationError(STALE)
        payment = Payment.objects.select_for_update(of=("self",)).select_related("participant__team").get(pk=session.payment_id)
        if (
            session.used_at or session.expires_at <= timezone.now()
            or payment.status != Payment.Status.REVIEW or not payment.submitted_at
            or payment.submitted_at != session.submitted_at_snapshot
        ):
            raise ValidationError(STALE)
        session.payment = payment
        return session

    def _callback(self, query):
        sender = query.get("from", {})
        chat = query.get("message", {}).get("chat", {})
        if not self._authorized(sender, chat):
            self._answer(query, DENIED)
            return
        parts = str(query.get("data", "")).split(":")
        if len(parts) != 3 or parts[0] != "pr" or parts[1] not in {"a", "r", "y"}:
            self._answer(query, STALE)
            return
        prompt_text = None
        try:
            with transaction.atomic():
                session = self._session(parts[2], sender, chat)
                if parts[1] == "y":
                    if not session.prompt_message_id or (session.action == "approve" and not session.bank_reference) or (session.action == "reject" and not session.reason):
                        raise ValidationError(STALE)
                    review_payment(
                        session.payment_id, approve=session.action == "approve",
                        bank_reference=session.bank_reference, reason=session.reason,
                        actor_telegram=sender["id"],
                    )
                    session.used_at = timezone.now()
                    session.save(update_fields=["used_at"])
                    answer = "Решение сохранено."
                else:
                    expected = "approve" if parts[1] == "a" else "reject"
                    if session.action != expected:
                        raise ValidationError(STALE)
                    if session.prompt_message_id:
                        answer = "Ответьте на ранее отправленный запрос либо откройте /pending."
                    else:
                        instruction = (
                            "Откройте фактическое поступление в Kaspi и сверьте сумму, участника и код. "
                            "Ответом именно на это сообщение отправьте уникальный номер банковской операции "
                            "из Kaspi (не код CQC и не номер со скриншота участника)."
                            if session.action == "approve" else
                            "Ответом именно на это сообщение отправьте причину отклонения."
                        )
                        prompt_text = self._summary(session.payment) + "\n\n" + instruction
                        answer = "Отправлен запрос для сверки."
            # Do not hold SQLite's IMMEDIATE writer lock across HTTP requests.
            # If delivery/commit is interrupted, replay sends a fresh prompt;
            # only the persisted message ID is accepted as a reply target.
            if prompt_text:
                response = self._send(prompt_text, reply_markup={"force_reply": True, "selective": True})
                with transaction.atomic():
                    session = self._session(parts[2], sender, chat)
                    if session.prompt_message_id is None:
                        session.prompt_message_id = response["message_id"]
                        session.save(update_fields=["prompt_message_id"])
        except (ValidationError, PermissionDenied) as exc:
            answer = " ".join(exc.messages) if isinstance(exc, ValidationError) else DENIED
        self._answer(query, answer)

    def _reply(self, message):
        prompt_id = message.get("reply_to_message", {}).get("message_id")
        session = TelegramReviewSession.objects.filter(
            chat_id=self.cashier_id, user_id=self.cashier_id, prompt_message_id=prompt_id,
        ).first() if type(prompt_id) is int else None
        if not session:
            self._send(STALE)
            return
        try:
            with transaction.atomic():
                session = self._session(str(session.pk), message.get("from", {}), message.get("chat", {}))
                value = str(message.get("text", "")).strip()
                if not value or len(value) > (150 if session.action == "approve" else 500):
                    raise ValidationError("Отправьте текст: номер операции до 150 символов или причину до 500 символов.")
                if session.action == "approve":
                    value = value.upper()
                    if value.startswith("CQC-"):
                        raise ValidationError("Нужен уникальный номер операции из Kaspi, а не код участника CQC.")
                    if session.bank_reference and session.bank_reference != value:
                        raise ValidationError("Ответ уже зафиксирован. Для нового решения откройте /pending.")
                    session.bank_reference = value
                    label = "☑ Сверил Kaspi — подтвердить"
                    details = f"Банковская операция: {value}\nПодтверждаю, что лично сверил фактическое поступление, сумму, участника и код в Kaspi."
                else:
                    if session.reason and session.reason != value:
                        raise ValidationError("Ответ уже зафиксирован. Для нового решения откройте /pending.")
                    session.reason = value
                    label = "Подтверждаю отклонение"
                    details = f"Причина: {value}"
                session.save(update_fields=["bank_reference", "reason"])
                confirmation_text = self._summary(session.payment) + "\n\n" + details + "\n\nНажатие кнопки сохранит решение."
            # Freeze the choice before sending, and allow identical replies to
            # resend this same confirmation after a network error or restart.
            self._send(confirmation_text,
                       reply_markup={"inline_keyboard": [[{"text": label, "callback_data": f"pr:y:{session.pk.hex}"}]]})
        except (ValidationError, PermissionDenied) as exc:
            self._send(" ".join(exc.messages) if isinstance(exc, ValidationError) else DENIED)

    def handle_update(self, update):
        from .telegram_auth import handle_auth_update
        if handle_auth_update(self, update):
            return
        if "callback_query" in update:
            if settings.DEMO_MODE:
                self._answer(update['callback_query'], 'Тестовый режим: проверяйте тестовые взносы в панели организатора сайта.')
                return
            self._callback(update["callback_query"])
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender, chat = message.get("from", {}), message.get("chat", {})
        # Ignore groups completely: no IDs, roles or payment data are sent there.
        if chat.get("type") != "private" or chat.get("id") != sender.get("id"):
            return
        command = str(message.get("text", "")).split(maxsplit=1)[0] if message.get("text") else ""
        command = command.split("@", 1)[0]
        authorized = self._authorized(sender, chat)
        if command in {"/start", "/id", "/help"}:
            role = "кассир" if authorized else "участник (без права проверки платежей)"
            guidance = (
                "\n\nДля входа откройте сайт турнира и нажмите «Войти через Telegram». "
                "Затем вернитесь в этот чат по кнопке сайта."
                if command in {"/start", "/help"} else ""
            )
            self._send(f"Ваш Telegram ID: {sender.get('id')}\nРоль: {role}." + guidance + ("\n/pending — платежи на проверке." if authorized else ""), chat_id=chat["id"])
        elif not authorized:
            self._send(DENIED, chat_id=chat["id"])
        elif settings.DEMO_MODE:
            self._send('Тестовый режим: реальные платежи не принимаются. Тестовые взносы проверяются в панели организатора сайта.')
        elif command == "/pending":
            payments = list(Payment.objects.filter(status=Payment.Status.REVIEW).select_related("participant__team").order_by("submitted_at", "pk")[:20])
            self._send(f"На проверке показано: {len(payments)} (до 20). Для каждой оплаты требуется сверка в Kaspi.")
            for payment in payments:
                self.send_review_card(payment)
        elif message.get("reply_to_message"):
            self._reply(message)
        else:
            self._send("/pending — платежи на проверке. Для ответа используйте «Ответить» на запрос бота.")

    def poll_once(self):
        state, _ = BotState.objects.get_or_create(key="telegram_update_offset", defaults={"value": "0"})
        offset = int(state.value)
        updates = self.client.call("getUpdates", offset=offset, timeout=15, allowed_updates=["message", "callback_query"])
        for update in updates:
            if type(update.get("update_id")) is not int or update["update_id"] < offset:
                continue
            try:
                self.handle_update(update)
            except TelegramError as exc:
                if not exc.permanent_delivery_failure:
                    raise
                # A user can block the bot after issuing /start. Their 403
                # must not indefinitely prevent processing the cashier queue.
                logger.warning("sendMessage permanently unavailable; update processed")
            offset = update["update_id"] + 1
            BotState.objects.filter(pk=state.pk).update(value=str(offset))
