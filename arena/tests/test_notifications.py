import uuid
from datetime import timedelta
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.models import Match, Participant, Team, TelegramIdentity, Tournament
from arena.notification_models import Notification
from arena.notifications import notify_match, notify_teams, notify_users, queue_readiness_reminders, safe_local_path, send_pending_notifications
from arena.telegram import TelegramError


@override_settings(DEMO_MODE=False, EVENT_TELEGRAM_ENABLED=True, PUBLIC_BASE_URL="https://example.test", SECURE_SSL_REDIRECT=False)
class NotificationTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.captain = users.objects.create_user("captain")
        self.member = users.objects.create_user("member")
        self.other = users.objects.create_user("other")
        self.former = users.objects.create_user("former")
        TelegramIdentity.objects.create(user=self.captain, telegram_id=101)
        TelegramIdentity.objects.create(user=self.member, telegram_id=102)
        self.tournament = Tournament.objects.create()
        self.team = Team.objects.create(tournament=self.tournament, captain=self.captain, name="A", contact="SECRET", status=Team.Status.CONFIRMED)
        self.team_b = Team.objects.create(tournament=self.tournament, captain=self.other, name="B", contact="SECRET", status=Team.Status.CONFIRMED)
        Participant.objects.create(team=self.team, account=self.member, nickname="Player", game_id="1", server_id="1")
        Participant.objects.create(team=self.team, account=self.captain, nickname="Captain", game_id="2", server_id="1")
        Participant.objects.create(team=self.team, account=self.former, active=False, nickname="Former", game_id="3", server_id="1")
        self.match = Match.objects.create(tournament=self.tournament, key="U1-1", bracket="upper", round=1, index=1, best_of=3, team_a=self.team, team_b=self.team_b)
        self.telegram = MagicMock()
        self.telegram.call.return_value = {"message_id": 100}

    def event(self, key="one"):
        return notify_users([self.captain.pk], event_key=key, title="Матч назначен", body="Откройте матч", path=f"/matches/{self.match.pk}/")[0]

    def test_team_recipients_only_active_members_and_captain_no_duplicates(self):
        items = notify_teams([self.team.pk, self.team.pk], event_key="team:decision", title="Допуск")
        self.assertEqual({x.user_id for x in items}, {self.captain.pk, self.member.pk})
        self.assertEqual(notify_teams([self.team.pk], event_key="team:decision", title="Другой текст"), [])
        self.assertEqual(Notification.objects.count(), 2)

    def test_inactive_users_and_unverified_contacts_are_not_external_recipients(self):
        self.member.is_active = False
        self.member.save(update_fields=["is_active"])
        items = notify_teams([self.team.pk, self.team_b.pk], event_key="x", title="Новость")
        self.assertEqual({x.user_id for x in items}, {self.captain.pk, self.other.pk})
        self.assertIsNone(Notification.objects.get(user=self.other).telegram_id)

    def test_queue_rolls_back_with_business_transaction(self):
        try:
            with transaction.atomic():
                self.event()
                raise ValidationError("business rollback")
        except ValidationError:
            pass
        self.assertEqual(Notification.objects.count(), 0)

    def test_path_rejects_external_and_obfuscated_links(self):
        for path in ["https://evil.test", "//evil.test", "/%2fevil.test", "/\\evil.test", "/%5cevil.test", "/%0aevil", "javascript:alert(1)"]:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                safe_local_path(path)
        self.assertEqual(safe_local_path("/matches/1/?tab=result"), "/matches/1/?tab=result")

    def test_match_event_targets_both_teams_without_lobby_or_contact(self):
        self.match.lobby_details = "SECRET LOBBY"
        items = notify_match(self.match, event_key="scheduled", title="Матч назначен")
        self.assertEqual({x.user_id for x in items}, {self.captain.pk, self.member.pk, self.other.pk})
        self.assertTrue(all(x.path == f"/matches/{self.match.pk}/" and "SECRET" not in x.body for x in items))

    def test_demo_and_opt_out_never_call_telegram(self):
        item = self.event()
        for options in [{"DEMO_MODE": True}, {"EVENT_TELEGRAM_ENABLED": False}]:
            with self.settings(**options):
                self.assertTrue(send_pending_notifications(client=self.telegram)["disabled"])
        self.telegram.call.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.attempts, 0)

    def test_delivery_records_success_and_does_not_resend(self):
        item = self.event()
        self.assertEqual(send_pending_notifications(client=self.telegram)["sent"], 1)
        self.assertEqual(send_pending_notifications(client=self.telegram)["sent"], 0)
        self.telegram.call.assert_called_once()
        payload = self.telegram.call.call_args.kwargs
        self.assertEqual(payload["chat_id"], 101)
        self.assertNotIn("parse_mode", payload)
        item.refresh_from_db()
        self.assertIsNotNone(item.sent_at)
        self.assertEqual(item.attempts, 1)
        self.assertIsNone(item.claim_token)

    def test_transient_failure_has_backoff_and_safe_error_then_retries(self):
        item = self.event()
        self.telegram.call.side_effect = TelegramError("SECRET network token")
        self.assertEqual(send_pending_notifications(client=self.telegram)["failed"], 1)
        item.refresh_from_db()
        self.assertIsNone(item.sent_at)
        self.assertNotIn("SECRET", item.last_error)
        self.assertIsNotNone(item.next_attempt_at)
        self.assertEqual(send_pending_notifications(client=self.telegram)["failed"], 0)
        Notification.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.telegram.call.side_effect = None
        self.assertEqual(send_pending_notifications(client=self.telegram)["sent"], 1)

    def test_permanent_block_stops_external_delivery_but_keeps_inbox(self):
        item = self.event()
        self.telegram.call.side_effect = TelegramError("forbidden", method="sendMessage", status=403)
        send_pending_notifications(client=self.telegram)
        item.refresh_from_db()
        self.assertIsNotNone(item.delivery_stopped_at)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(send_pending_notifications(client=self.telegram)["failed"], 0)

    def test_relinked_telegram_never_receives_old_private_events(self):
        item = self.event()
        TelegramIdentity.objects.filter(user=self.captain).update(telegram_id=999)
        send_pending_notifications(client=self.telegram)
        self.telegram.call.assert_not_called()
        item.refresh_from_db()
        self.assertIsNotNone(item.delivery_stopped_at)

    def test_active_delivery_claim_is_skipped_expired_claim_can_recover(self):
        item = self.event()
        Notification.objects.filter(pk=item.pk).update(claim_token=uuid.uuid4(), claimed_until=timezone.now() + timedelta(minutes=2))
        send_pending_notifications(client=self.telegram)
        self.telegram.call.assert_not_called()
        Notification.objects.filter(pk=item.pk).update(claimed_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(send_pending_notifications(client=self.telegram)["sent"], 1)

    def test_nested_worker_cannot_claim_same_live_delivery(self):
        self.event()
        other_client = MagicMock()
        self.telegram.call.side_effect = lambda *a, **kw: send_pending_notifications(client=other_client)
        self.assertEqual(send_pending_notifications(client=self.telegram)["sent"], 1)
        other_client.call.assert_not_called()

    def test_reminder_once_for_unready_captain_reschedule_gets_new_event(self):
        now = timezone.now()
        self.match.ready_deadline = now + timedelta(minutes=10)
        self.match.ready_b_at = now
        self.match.save()
        self.assertEqual(queue_readiness_reminders(now), 1)
        self.assertEqual(queue_readiness_reminders(now), 0)
        item = Notification.objects.get()
        self.assertEqual(item.user, self.captain)
        self.match.ready_deadline += timedelta(minutes=1)
        self.match.save()
        self.assertEqual(queue_readiness_reminders(now), 1)

    def test_no_reminder_after_deadline_or_when_match_incomplete_started_done(self):
        now = timezone.now()
        for fields in [
            {"ready_deadline": now - timedelta(minutes=1)},
            {"ready_deadline": now + timedelta(minutes=20)},
            {"ready_deadline": now + timedelta(minutes=5), "team_b_id": None},
            {"ready_deadline": now + timedelta(minutes=5), "started_at": now},
            {"ready_deadline": now + timedelta(minutes=5), "completed_at": now},
        ]:
            with self.subTest(fields=fields):
                Match.objects.filter(pk=self.match.pk).update(ready_deadline=None, team_b_id=self.team_b.pk, started_at=None, completed_at=None)
                Match.objects.filter(pk=self.match.pk).update(**fields)
                self.assertEqual(queue_readiness_reminders(now), 0)

    def test_inbox_is_private_read_post_is_owned_and_idempotent(self):
        item = self.event()
        notify_users([self.other.pk], event_key="private", title="OTHER PRIVATE")
        self.assertEqual(self.client.get(reverse("notifications")).status_code, 302)
        self.client.force_login(self.captain)
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "Матч назначен")
        self.assertNotContains(response, "OTHER PRIVATE")
        url = reverse("notification_read", args=[item.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 302)
        item.refresh_from_db()
        read_at = item.read_at
        self.client.post(url)
        item.refresh_from_db()
        self.assertEqual(read_at, item.read_at)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_read_requires_csrf(self):
        item = self.event()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.captain)
        self.assertEqual(csrf.post(reverse("notification_read", args=[item.pk])).status_code, 403)

    def test_inbox_refresh_only_enqueues_current_captain_reminder(self):
        self.match.ready_deadline = timezone.now() + timedelta(minutes=5)
        self.match.save()
        self.client.force_login(self.captain)
        self.client.get(reverse("notifications"))
        self.assertEqual(list(Notification.objects.values_list("user_id", flat=True)), [self.captain.pk])
