"""Durable event inbox and optional, at-least-once Telegram delivery.

Queue events inside the business transaction. Telegram is never called there.
Only the explicit management command sends, and both an opt-in setting and a
non-demo environment are required. A transport timeout after acceptance can
still produce a duplicate: Telegram sendMessage has no idempotency key.
"""

import uuid
from datetime import timedelta
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .notification_models import Notification


def safe_local_path(path):
    path = str(path)
    decoded = unquote(path)
    if (not path.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or any(ord(ch) < 32 for ch in decoded) or len(path) > 250):
        raise ValidationError("Ссылка уведомления должна вести на этот сайт.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise ValidationError("Ссылка уведомления должна вести на этот сайт.")
    return path


@transaction.atomic
def notify_users(user_ids, *, event_key, title, body="", path="/schedule/"):
    from django.contrib.auth import get_user_model
    from .models import TelegramIdentity

    event_key, title, body = str(event_key).strip(), str(title).strip(), str(body).strip()
    if not event_key or len(event_key) > 200 or not title or len(title) > 180 or len(body) > 2000:
        raise ValidationError("Некорректное уведомление.")
    path = safe_local_path(path)
    recipients = list(get_user_model().objects.filter(pk__in=set(user_ids), is_active=True).values_list("pk", flat=True))
    identities = dict(TelegramIdentity.objects.filter(user_id__in=recipients).values_list("user_id", "telegram_id"))
    created = []
    for user_id in recipients:
        item, is_new = Notification.objects.get_or_create(
            user_id=user_id, event_key=event_key,
            defaults={"title": title, "body": body, "path": path, "telegram_id": identities.get(user_id)},
        )
        if is_new:
            created.append(item)
    return created


def notify_teams(team_ids, *, event_key, title, body="", path="/schedule/"):
    from .models import Participant, Team

    ids = {value for value in team_ids if value is not None}
    recipients = set(Team.objects.filter(pk__in=ids).values_list("captain_id", flat=True))
    recipients.update(Participant.objects.filter(team_id__in=ids, active=True, account_id__isnull=False)
                      .values_list("account_id", flat=True))
    return notify_users(recipients, event_key=event_key, title=title, body=body, path=path)


def notify_match(match, *, event_key, title, body=""):
    return notify_teams([match.team_a_id, match.team_b_id], event_key=event_key,
                        title=title, body=body, path=f"/matches/{match.pk}/")


@transaction.atomic
def queue_readiness_reminders(now=None, *, user_id=None):
    """Notify unready captains once in the 15-minute window before a deadline.

    No guessed tournament deadline and no automatic forfeits. A scheduler must
    run the management command regularly; rescheduling yields a new event key.
    """
    from .models import Match

    now = now or timezone.now()
    count = 0
    matches = Match.objects.filter(active=True, completed_at__isnull=True, started_at__isnull=True,
                                   ready_deadline__gt=now, ready_deadline__lte=now + timedelta(minutes=15),
                                   team_a_id__isnull=False, team_b_id__isnull=False)
    if user_id is not None:
        matches = matches.filter(Q(team_a__captain_id=user_id) | Q(team_b__captain_id=user_id))
    for match in matches.select_related("team_a", "team_b").order_by("ready_deadline", "pk")[:100]:
        recipients = []
        for side in ("a", "b"):
            captain_id = getattr(match, f"team_{side}").captain_id
            if not getattr(match, f"ready_{side}_at") and (user_id is None or captain_id == user_id):
                recipients.append(captain_id)
        count += len(notify_users(
            recipients, event_key=f"match:{match.pk}:ready:{match.ready_deadline.isoformat()}",
            title="Подтвердите готовность к матчу",
            body="До назначенного срока готовности осталось не более 15 минут. Откройте карточку матча.",
            path=f"/matches/{match.pk}/",
        ))
    return count


def _available(now):
    return Notification.objects.filter(sent_at__isnull=True, delivery_stopped_at__isnull=True,
                                       telegram_id__isnull=False).filter(
        Q(claimed_until__isnull=True) | Q(claimed_until__lte=now),
    ).filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))


def send_pending_notifications(limit=20, client=None):
    """Claim by compare-and-set; network requests happen outside transactions."""
    from .models import TelegramIdentity
    from .telegram import TelegramClient, TelegramError

    if settings.DEMO_MODE or not getattr(settings, "EVENT_TELEGRAM_ENABLED", False):
        return {"sent": 0, "failed": 0, "disabled": True}
    client = client or TelegramClient()
    sent = failed = 0
    now = timezone.now()
    ids = list(_available(now).order_by("created_at", "pk").values_list("pk", flat=True)[:max(0, min(limit, 100))])
    for item_id in ids:
        token = uuid.uuid4()
        claimed = _available(timezone.now()).filter(pk=item_id).update(
            claim_token=token, claimed_until=timezone.now() + timedelta(minutes=2), attempts=F("attempts") + 1,
        )
        if not claimed:
            continue
        item = Notification.objects.get(pk=item_id, claim_token=token)
        owned = Notification.objects.filter(pk=item.pk, claim_token=token)
        if not TelegramIdentity.objects.filter(user_id=item.user_id, telegram_id=item.telegram_id, user__is_active=True).exists():
            owned.update(delivery_stopped_at=timezone.now(), claim_token=None, claimed_until=None,
                         last_error="Verified Telegram identity changed; delivery stopped")
            continue
        try:
            path = safe_local_path(item.path)
            base = settings.PUBLIC_BASE_URL.rstrip("/")
            parsed = urlsplit(base)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValidationError("Telegram links require a public HTTPS origin")
            # No bank, lobby or contact secrets in message bodies. Plain text,
            # with no parse_mode; arbitrary names cannot inject markup.
            text = f"CQC · {item.title}\n\n{item.body}\n\n{base}{path}".strip()
            client.call("sendMessage", chat_id=item.telegram_id, text=text,
                        link_preview_options={"is_disabled": True})
        except (TelegramError, ValidationError) as exc:
            failed += 1
            permanent = isinstance(exc, ValidationError) or getattr(exc, "permanent_delivery_failure", False)
            owned.update(claim_token=None, claimed_until=None,
                         next_attempt_at=timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** min(item.attempts, 6))),
                         delivery_stopped_at=timezone.now() if permanent else None,
                         last_error="Delivery stopped" if permanent else "Delivery failed; retry scheduled")
        else:
            sent += 1
            owned.update(sent_at=timezone.now(), claim_token=None, claimed_until=None, last_error="")
    return {"sent": sent, "failed": failed, "disabled": False}
