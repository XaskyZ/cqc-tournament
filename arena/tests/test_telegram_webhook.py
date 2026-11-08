import json
from datetime import timedelta
from threading import Event, Thread
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.models import BotState, TelegramIdentity, TelegramLoginChallenge
from arena.telegram import TelegramError
from arena.telegram_auth import SESSION_KEY
from arena.telegram_webhook import (
    LOCK_KEY, MAX_BODY_BYTES, RECEIPT_PREFIX, LeasedTelegramClient,
    WebhookBusy, _claim, _release, process_update,
)


@override_settings(DEMO_MODE=True, TELEGRAM_WEBHOOK_SECRET="test_webhook_secret",
                   TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_BOT_USERNAME="test_bot",
                   TELEGRAM_CASHIER_ID=855341070)
class TelegramWebhookTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("telegram_webhook")
        self.api = MagicMock()
        self.api.call.return_value = {"message_id": 100}
        factory = patch("arena.telegram_webhook.TelegramClient", return_value=self.api)
        factory.start()
        self.addCleanup(factory.stop)
        network = patch("arena.telegram.urlopen", side_effect=AssertionError("Real Telegram HTTP forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def post(self, update=None, *, secret="test_webhook_secret", browser=None, **extra):
        return (browser or self.client).post(
            self.url, json.dumps(update if update is not None else {"update_id": 1}),
            content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret, **extra,
        )

    def message(self, text="/start", update_id=1, reply=None):
        message = {"text": text, "from": {"id": 101, "first_name": "Player"},
                   "chat": {"id": 101, "type": "private"}}
        if reply is not None:
            message["reply_to_message"] = {"message_id": reply}
        return {"update_id": update_id, "message": message}

    def test_secret_is_required_before_any_database_or_api_work(self):
        for secret in ("", "wrong", "тест"):
            with self.subTest(secret=secret):
                self.assertEqual(self.post(self.message(), secret=secret).status_code, 403)
        with override_settings(TELEGRAM_WEBHOOK_SECRET=""):
            self.assertEqual(self.post().status_code, 503)
        with override_settings(TELEGRAM_WEBHOOK_SECRET="invalid secret"):
            self.assertEqual(self.post().status_code, 503)
        self.assertFalse(BotState.objects.exists())
        self.api.call.assert_not_called()

    def test_only_post_json_bounded_body_and_valid_update(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        header = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "test_webhook_secret"}
        self.assertEqual(self.client.post(self.url, "{}", content_type="text/plain", **header).status_code, 415)
        for body in ("{", "[]", '{"update_id": true}', '{"update_id": -1}',
                     '{"update_id":1,"message":null}', '{"update_id":1,"message":{"from":[]}}',
                     '{"update_id":1,"callback_query":{"id":"q","message":{"chat":null}}}'):
            self.assertEqual(self.client.post(self.url, body, content_type="application/json", **header).status_code, 400)
        self.assertEqual(self.client.post(self.url, " " * (MAX_BODY_BYTES + 1), content_type="application/json", **header).status_code, 413)
        self.api.call.assert_not_called()

    def test_secret_authenticated_webhook_is_csrf_exempt(self):
        response = self.post(self.message(), browser=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.api.call.assert_called_once()

    def test_completed_duplicate_never_processes_again_and_out_of_order_is_not_dropped(self):
        self.assertEqual(self.post(self.message(update_id=20)).status_code, 200)
        self.assertEqual(self.post(self.message(update_id=20)).status_code, 200)
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.post(self.message(update_id=3)).status_code, 200)
        self.assertEqual(self.api.call.call_count, 2)
        self.assertEqual(BotState.objects.filter(key__startswith=RECEIPT_PREFIX).count(), 2)
        self.assertFalse(BotState.objects.filter(pk="telegram_update_offset").exists())

    def test_live_lease_returns_retry_without_discarding_incoming_update(self):
        lease = _claim(1)
        self.assertIsNotNone(lease)
        response = self.post(self.message(update_id=2))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "5")
        self.assertFalse(BotState.objects.filter(pk=f"{RECEIPT_PREFIX}2").exists())
        self.api.call.assert_not_called()
        _release(lease)
        self.assertEqual(self.post(self.message(update_id=2)).status_code, 200)

    def test_completed_duplicate_is_acknowledged_even_while_other_update_runs(self):
        self.post(self.message(update_id=1))
        lease = _claim(2)
        self.assertEqual(self.post(self.message(update_id=1)).status_code, 200)
        self.assertEqual(BotState.objects.get(pk=LOCK_KEY).value, lease)

    def test_expired_crashed_worker_can_be_reclaimed_and_cannot_unlock_new_owner(self):
        old = json.dumps({"owner": "dead", "expires": (timezone.now() - timedelta(seconds=1)).isoformat()})
        BotState.objects.create(key=LOCK_KEY, value=old)
        new = _claim(2)
        self.assertNotEqual(new, old)
        _release(old)
        self.assertEqual(BotState.objects.get(pk=LOCK_KEY).value, new)

    def test_lease_guard_refuses_http_near_expiry_or_after_owner_replacement(self):
        near = json.dumps({"owner": "slow", "expires": (timezone.now() + timedelta(seconds=10)).isoformat()})
        BotState.objects.create(key=LOCK_KEY, value=near)
        with self.assertRaises(WebhookBusy):
            LeasedTelegramClient(near).call("sendMessage", text="test")
        _release(near)
        fresh = _claim(2)
        _release(fresh)
        with self.assertRaises(WebhookBusy):
            LeasedTelegramClient(fresh).call("sendMessage", text="test")
        self.api.call.assert_not_called()

    def test_network_is_outside_transaction_and_overlapping_request_is_deferred(self):
        def api(method, **payload):
            self.assertFalse(connection.in_atomic_block)
            with patch("arena.telegram_webhook.CashierBot.handle_update") as nested_handler:
                self.assertEqual(self.post(self.message(update_id=2)).status_code, 503)
                nested_handler.assert_not_called()
            return {"message_id": 100}

        self.api.call.side_effect = api
        self.assertEqual(self.post(self.message()).status_code, 200)
        self.assertEqual(BotState.objects.get(pk=LOCK_KEY).value, "")

    def test_transient_failure_releases_lease_without_receipt_and_retry_succeeds(self):
        self.api.call.side_effect = TelegramError("private token URL")
        response = self.post(self.message())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private", response.content)
        self.assertFalse(BotState.objects.filter(pk=f"{RECEIPT_PREFIX}1").exists())
        self.assertEqual(BotState.objects.get(pk=LOCK_KEY).value, "")
        self.api.call.side_effect = None
        self.assertEqual(self.post(self.message()).status_code, 200)

    def test_permanent_delivery_failure_is_recorded_and_does_not_poison_next_update(self):
        self.api.call.side_effect = TelegramError("blocked", method="sendMessage", status=403)
        self.assertEqual(self.post(self.message()).status_code, 200)
        self.assertTrue(BotState.objects.filter(pk=f"{RECEIPT_PREFIX}1").exists())
        self.api.call.side_effect = None
        self.assertEqual(self.post(self.message(update_id=2)).status_code, 200)

    def test_demo_login_full_flow_and_financial_callbacks_stay_disabled(self):
        self.assertEqual(self.client.post(reverse("telegram_auth_begin"), {"purpose": "login"}).status_code, 302)
        saved = self.client.session[SESSION_KEY]
        challenge = TelegramLoginChallenge.objects.get(pk=saved["id"])
        self.assertEqual(self.post(self.message("/start login_" + saved["nonce"])).status_code, 200)
        self.assertEqual(self.post(self.message(saved["code"], update_id=2, reply=100)).status_code, 200)
        callback = {"update_id": 3, "callback_query": {"id": "query", "data": f"ta:y:{challenge.pk.hex}",
                    "from": {"id": 101}, "message": {"chat": {"id": 101, "type": "private"}}}}
        self.assertEqual(self.post(callback).status_code, 200)
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.approved_at)
        self.assertFalse(TelegramIdentity.objects.exists())
        self.assertEqual(self.client.post(reverse("telegram_auth_finish"), {"confirmed": "on", "telegram_id": "101"}).status_code, 302)
        self.assertTrue(TelegramIdentity.objects.filter(telegram_id=101).exists())
        callback["update_id"] = 4
        callback["callback_query"]["data"] = "pr:y:forged"
        with patch("arena.telegram.CashierBot._callback") as financial, patch("arena.telegram.CashierBot.flush_outbox") as flush:
            self.assertEqual(self.post(callback).status_code, 200)
            financial.assert_not_called()
            flush.assert_not_called()

    def test_failed_wrong_code_response_retry_counts_one_auth_attempt(self):
        self.client.post(reverse("telegram_auth_begin"), {"purpose": "login"})
        saved = self.client.session[SESSION_KEY]
        self.post(self.message("/start login_" + saved["nonce"]))
        wrong_answer = self.message("not-a-six-digit-code", update_id=2, reply=100)
        self.api.call.side_effect = TelegramError("temporary transport failure")
        self.assertEqual(self.post(wrong_answer).status_code, 503)
        challenge = TelegramLoginChallenge.objects.get(pk=saved["id"])
        self.assertEqual(challenge.attempts, 1)
        self.api.call.side_effect = None
        self.assertEqual(self.post(wrong_answer).status_code, 200)
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 1)
        self.assertIsNone(challenge.cancelled_at)
        wrong_answer["update_id"] = 3
        self.assertEqual(self.post(wrong_answer).status_code, 200)
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 2)

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locking")
    def test_concurrent_postgres_workers_only_process_update_once(self):
        entered, release = Event(), Event()
        errors = []

        def handle(update):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release worker")

        def first_worker():
            close_old_connections()
            try:
                process_update(self.message())
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        with patch("arena.telegram_webhook.CashierBot.handle_update", side_effect=handle) as handler:
            thread = Thread(target=first_worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(WebhookBusy):
                    process_update(self.message())
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            process_update(self.message())
            self.assertEqual(handler.call_count, 1)
