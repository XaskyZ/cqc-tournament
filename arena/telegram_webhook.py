"""Authenticated Telegram ingress, with durable receipts and a short DB lease.

Set the webhook separately with allowed_updates=[message, callback_query] and
max_connections=1. Never run polling concurrently. Receipts handle out-of-order
updates; retries can repeat a Telegram message after an ambiguous network error,
but domain decisions retain their existing transactional/idempotency checks.
No payment outbox is drained here: production delivery needs a separate worker.
"""
import hmac
import json
import logging
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import BotState
from .telegram import CashierBot, TelegramClient, TelegramError

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024
LEASE_SECONDS = 120
# TelegramClient's transport timeout is 25s. Do not begin another call near the
# lease boundary. A killed serverless invocation is recovered after lease expiry.
REQUEST_RESERVE_SECONDS = 30
LOCK_KEY = "telegram_webhook_lease"
RECEIPT_PREFIX = "telegram_webhook_done:"


class WebhookBusy(Exception):
    pass


def _live_lease(value, now):
    if not value:
        return False
    try:
        expires = parse_datetime(json.loads(value)["expires"])
        return expires is not None and timezone.is_aware(expires) and expires > now
    except (ValueError, TypeError, KeyError):
        # A corrupted lock is an operational failure, not permission to overlap.
        raise WebhookBusy from None


def _claim(update_id):
    with transaction.atomic():
        state, _ = BotState.objects.get_or_create(key=LOCK_KEY, defaults={"value": ""})
        state = BotState.objects.select_for_update().get(pk=state.pk)
        if BotState.objects.filter(pk=f"{RECEIPT_PREFIX}{update_id}").exists():
            return None
        now = timezone.now()
        if _live_lease(state.value, now):
            raise WebhookBusy
        value = json.dumps({"owner": uuid.uuid4().hex, "expires": (now + timedelta(seconds=LEASE_SECONDS)).isoformat()})
        # CAS also protects engines without SELECT FOR UPDATE, including SQLite.
        if BotState.objects.filter(pk=LOCK_KEY, value=state.value).update(value=value) != 1:
            raise WebhookBusy
        return value


def _release(lease):
    BotState.objects.filter(pk=LOCK_KEY, value=lease).update(value="")


def _complete(update_id, lease):
    with transaction.atomic():
        state = BotState.objects.select_for_update().get(pk=LOCK_KEY)
        if state.value != lease or not _live_lease(lease, timezone.now()):
            raise WebhookBusy
        BotState.objects.get_or_create(key=f"{RECEIPT_PREFIX}{update_id}", defaults={"value": timezone.now().isoformat()})
        _release(lease)


class LeasedTelegramClient:
    def __init__(self, lease):
        self.lease = lease
        self.client = TelegramClient()

    def call(self, method, **payload):
        if (not _live_lease(self.lease, timezone.now() + timedelta(seconds=REQUEST_RESERVE_SECONDS))
                or not BotState.objects.filter(pk=LOCK_KEY, value=self.lease).exists()):
            raise WebhookBusy
        result = self.client.call(method, **payload)
        # Do not let a timed-out invocation bind a prompt after another worker
        # reclaimed its lease. Its next retry will send and bind a fresh prompt.
        if (not _live_lease(self.lease, timezone.now())
                or not BotState.objects.filter(pk=LOCK_KEY, value=self.lease).exists()):
            raise WebhookBusy
        return result


def process_update(update):
    lease = _claim(update["update_id"])
    if lease is None:
        return
    try:
        try:
            CashierBot(LeasedTelegramClient(lease)).handle_update(update)
        except TelegramError as exc:
            if not exc.permanent_delivery_failure:
                raise
            logger.warning("Webhook response permanently unavailable; update processed")
        _complete(update["update_id"], lease)
    finally:
        # Matching the unique owner prevents an overdue invocation clearing a
        # replacement worker's lease. No request body or secret is persisted.
        _release(lease)


def _valid_update(update):
    if (not isinstance(update, dict) or type(update.get("update_id")) is not int
            or not 0 <= update["update_id"] <= 2**63 - 1):
        return False
    for name in ("message", "callback_query"):
        if name in update and not isinstance(update[name], dict):
            return False
    message = update.get("message", {})
    query = update.get("callback_query", {})
    if message and query:
        return False
    for value in (message, query):
        if "from" in value and not isinstance(value["from"], dict):
            return False
    for name in ("chat", "reply_to_message"):
        if name in message and not isinstance(message[name], dict):
            return False
    if query:
        if not isinstance(query.get("id"), str) or not query["id"]:
            return False
        if "message" in query:
            if not isinstance(query["message"], dict):
                return False
            if "chat" in query["message"] and not isinstance(query["message"]["chat"], dict):
                return False
    return True


def _response(status):
    response = JsonResponse({"ok": status == 200}, status=status)
    response["Cache-Control"] = "no-store"
    if status == 503:
        response["Retry-After"] = "5"
    return response


@csrf_exempt
@require_POST
def telegram_webhook(request):
    secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        return _response(503)
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("ascii")):
        return _response(403)
    if request.content_type != "application/json":
        return _response(415)
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
        if length < 0:
            return _response(400)
        if length > MAX_BODY_BYTES:
            return _response(413)
        raw = request.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return _response(413)
        update = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return _response(400)
    if not _valid_update(update):
        return _response(400)
    try:
        process_update(update)
    except (WebhookBusy, TelegramError, DatabaseError):
        # Never log exception text: transport errors can contain token URLs.
        logger.warning("Telegram webhook deferred; retry required")
        return _response(503)
    return _response(200)
