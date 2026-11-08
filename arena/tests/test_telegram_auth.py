from datetime import timedelta
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.models import GameAccount, Participant, Payment, Team, TelegramIdentity, TelegramLoginChallenge, Tournament
from arena.telegram import CashierBot, TelegramError
from arena.telegram_auth import SESSION_KEY


@override_settings(TELEGRAM_BOT_TOKEN="test-token", TELEGRAM_BOT_USERNAME="test_tournament_bot", TELEGRAM_CASHIER_ID=855341070)
class TelegramAuthTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.tournament = Tournament.objects.create()
        self.api = MagicMock()
        self.api.call.return_value = {"message_id": 100}
        self.bot = CashierBot(self.api)

    def begin(self, browser=None, purpose="login", **extra):
        browser = browser or self.client
        response = browser.post(reverse("telegram_auth_begin"), {"purpose": purpose, **extra})
        self.assertEqual(response.status_code, 302)
        saved = browser.session[SESSION_KEY]
        return saved, TelegramLoginChallenge.objects.get(pk=saved["id"])

    def message(self, text, user=101, reply=None, chat_type="private", chat_id=None):
        result = {"message": {"text": text, "from": {"id": user, "username": "player", "first_name": "Player"}, "chat": {"id": user if chat_id is None else chat_id, "type": chat_type}}}
        if reply is not None:
            result["message"]["reply_to_message"] = {"message_id": reply}
        return result

    def callback(self, challenge, user=101, action="y", chat_type="private", chat_id=None):
        return {"callback_query": {"id": "query", "data": f"ta:{action}:{challenge.pk.hex}", "from": {"id": user}, "message": {"chat": {"id": user if chat_id is None else chat_id, "type": chat_type}}}}

    def prove(self, saved, challenge, user=101):
        self.bot.handle_update(self.message("/start login_" + saved["nonce"], user=user))
        self.bot.handle_update(self.message(saved["code"], user=user, reply=100))
        self.bot.handle_update(self.callback(challenge, user=user))

    def finish(self, browser=None, user=101, confirmed="on"):
        return (browser or self.client).post(reverse("telegram_auth_finish"), {"telegram_id": user, "confirmed": confirmed})

    def test_full_flow_needs_code_bot_approval_and_browser_finish(self):
        saved, challenge = self.begin()
        self.assertEqual(len(saved["code"]), 6)
        state = self.client.get(reverse("telegram_auth_status")).json()
        self.assertEqual(state['progress'], 'waiting')
        self.assertIsNone(state['telegram'])
        self.assertNotIn(saved["code"], state["deep_link"])
        self.assertLessEqual(len("login_" + saved["nonce"]), 64)
        self.assertNotEqual(challenge.nonce_hash, saved["nonce"])
        self.assertNotEqual(challenge.code_hash, saved["code"])
        self.bot.handle_update(self.message("/start login_" + saved["nonce"]))
        opened = self.client.get(reverse('telegram_auth_status')).json()
        self.assertEqual(opened['progress'], 'bot_opened')
        self.assertIsNone(opened['telegram'])
        self.bot.handle_update(self.callback(challenge))
        challenge.refresh_from_db()
        self.assertIsNone(challenge.approved_at)
        self.assertEqual(self.client.get(reverse("telegram_auth_status")).json()["state"], "pending")
        self.bot.handle_update(self.message(saved["code"], reply=100))
        code_state = self.client.get(reverse('telegram_auth_status')).json()
        self.assertEqual(code_state['progress'], 'code_verified')
        self.assertIsNone(code_state['telegram'])
        self.assertFalse(TelegramIdentity.objects.exists())
        self.assertEqual(self.finish().status_code, 400)
        self.bot.handle_update(self.callback(challenge))
        state = self.client.get(reverse("telegram_auth_status")).json()
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["telegram"]["id"], 101)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.finish(confirmed="").status_code, 400)
        self.assertEqual(self.finish(user=202).status_code, 400)
        result = self.finish()
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.url, reverse("game_account"))
        identity = TelegramIdentity.objects.get(telegram_id=101)
        self.assertEqual(str(identity.user_id), self.client.session["_auth_user_id"])
        self.assertFalse(identity.user.has_usable_password())
        self.assertFalse(identity.user.is_staff)
        self.assertFalse(identity.user.is_superuser)
        self.assertEqual(self.finish().status_code, 400)
        self.assertEqual(TelegramIdentity.objects.count(), 1)

    def test_browser_binding_prevents_other_session_finish_and_poll(self):
        saved, challenge = self.begin()
        self.prove(saved, challenge)
        other = Client()
        self.assertEqual(other.get(reverse("telegram_auth_status")).json()["state"], "none")
        # Knowing the public challenge UUID alone does not bind another session.
        session = other.session
        session[SESSION_KEY] = {"id": saved["id"], "binding": "forged", "code": saved["code"], "nonce": saved["nonce"]}
        session.save()
        self.assertEqual(self.finish(browser=other).status_code, 400)
        self.assertFalse(TelegramIdentity.objects.exists())

    def test_wrong_reply_chat_or_sender_cannot_prove_identity(self):
        saved, challenge = self.begin()
        self.bot.handle_update(self.message("/start login_" + saved["nonce"]))
        for update in [self.message(saved["code"], user=202, reply=100), self.message(saved["code"], reply=999), self.message(saved["code"], reply=100, chat_type="group"), self.message(saved["code"], reply=100, chat_id=202)]:
            self.bot.handle_update(update)
        challenge.refresh_from_db()
        self.assertIsNone(challenge.code_verified_at)
        self.bot.handle_update(self.message(saved["code"], reply=100))
        for update in [self.callback(challenge, user=202), self.callback(challenge, chat_type="group"), self.callback(challenge, chat_id=202)]:
            self.bot.handle_update(update)
        challenge.refresh_from_db()
        self.assertIsNone(challenge.approved_at)

    def test_forwarded_link_without_browser_code_cannot_log_in(self):
        saved, challenge = self.begin()
        self.bot.handle_update(self.message("/start login_" + saved["nonce"]))
        wrong = "000000" if saved["code"] != "000000" else "999999"
        for _ in range(5):
            self.bot.handle_update(self.message(wrong, reply=100))
        challenge.refresh_from_db()
        self.assertEqual(challenge.attempts, 5)
        self.assertIsNotNone(challenge.cancelled_at)
        self.prove(saved, challenge)
        self.assertEqual(self.finish().status_code, 400)
        self.assertFalse(TelegramIdentity.objects.exists())

    def test_expired_cancelled_and_replaced_challenges_cannot_finish(self):
        saved, challenge = self.begin()
        self.prove(saved, challenge)
        TelegramLoginChallenge.objects.filter(pk=challenge.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(reverse("telegram_auth_status")).json()["state"], "expired")
        self.assertEqual(self.finish().status_code, 400)
        saved, challenge = self.begin()
        self.prove(saved, challenge)
        self.client.post(reverse("telegram_auth_cancel"))
        self.assertEqual(self.finish().status_code, 400)
        saved, challenge = self.begin()
        self.prove(saved, challenge)
        self.begin()
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.cancelled_at)
        self.bot.handle_update(self.callback(challenge))
        self.assertFalse(TelegramIdentity.objects.exists())

    def test_existing_captain_link_preserves_user_and_cannot_reassign(self):
        captain = get_user_model().objects.create_user("captain", password="test-password")
        self.client.force_login(captain)
        saved, challenge = self.begin(purpose="link")
        self.prove(saved, challenge)
        self.assertEqual(self.finish().status_code, 302)
        self.assertEqual(TelegramIdentity.objects.get(telegram_id=101).user_id, captain.pk)
        other = get_user_model().objects.create_user("other")
        self.client.force_login(other)
        saved, challenge = self.begin(purpose="link")
        self.prove(saved, challenge)
        self.assertEqual(self.finish().status_code, 400)
        self.assertEqual(TelegramIdentity.objects.get(telegram_id=101).user_id, captain.pk)
        self.assertEqual(TelegramIdentity.objects.count(), 1)

    def test_link_requires_same_current_browser_account(self):
        first = get_user_model().objects.create_user("first")
        second = get_user_model().objects.create_user("second")
        self.client.force_login(first)
        saved, challenge = self.begin(purpose="link")
        self.prove(saved, challenge)
        # Preserve the auth challenge while changing the session principal.
        self.client.force_login(second)
        session = self.client.session
        session[SESSION_KEY] = saved
        session.save()
        self.assertEqual(self.finish().status_code, 400)
        self.assertFalse(TelegramIdentity.objects.exists())

    def test_existing_telegram_login_does_not_match_username_or_promote_cashier(self):
        collision = get_user_model().objects.create_user("telegram_101", is_staff=True)
        saved, challenge = self.begin()
        self.prove(saved, challenge, user=855341070)
        self.assertEqual(self.finish(user=855341070).status_code, 302)
        identity = TelegramIdentity.objects.get(telegram_id=855341070)
        self.assertNotEqual(identity.user_id, collision.pk)
        self.assertFalse(identity.user.is_staff)
        self.client.logout()
        saved, challenge = self.begin()
        self.prove(saved, challenge, user=855341070)
        self.assertEqual(self.finish(user=855341070).status_code, 302)
        self.assertEqual(str(identity.user_id), self.client.session["_auth_user_id"])
        self.assertEqual(TelegramIdentity.objects.count(), 1)

    def test_inactive_linked_account_cannot_login(self):
        user = get_user_model().objects.create_user("inactive", is_active=False)
        TelegramIdentity.objects.create(user=user, telegram_id=101)
        saved, challenge = self.begin()
        self.prove(saved, challenge)
        self.assertEqual(self.finish().status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_state_changing_routes_require_csrf_and_post(self):
        browser = Client(enforce_csrf_checks=True)
        for name in ("telegram_auth_begin", "telegram_auth_finish", "telegram_auth_cancel"):
            self.assertEqual(browser.get(reverse(name)).status_code, 405)
            self.assertEqual(browser.post(reverse(name), {"purpose": "login"}).status_code, 403)
        self.assertFalse(TelegramLoginChallenge.objects.exists())

    def test_bot_network_calls_are_outside_transaction_and_retry_safe(self):
        saved, challenge = self.begin()

        def api(method, **payload):
            self.assertFalse(connection.in_atomic_block)
            return {"message_id": 100}

        self.api.call.side_effect = api
        self.bot.handle_update(self.message("/start login_" + saved["nonce"]))
        self.api.call.side_effect = TelegramError("transport failure")
        with self.assertRaises(TelegramError):
            self.bot.handle_update(self.message(saved["code"], reply=100))
        challenge.refresh_from_db()
        self.assertIsNotNone(challenge.code_verified_at)
        self.assertIsNone(challenge.approved_at)
        self.api.call.side_effect = api
        self.bot.handle_update(self.message(saved["code"], reply=100))
        self.bot.handle_update(self.callback(challenge))
        self.bot.handle_update(self.callback(challenge))
        self.assertEqual(self.finish().status_code, 302)
        self.assertEqual(TelegramIdentity.objects.count(), 1)

    def test_participant_return_is_fixed_and_never_auto_claims(self):
        captain = get_user_model().objects.create_user("captain")
        team = Team.objects.create(tournament=self.tournament, captain=captain, name="Team", contact="contact")
        participant = Participant.objects.create(team=team, nickname="Player", game_id="123", server_id="456")
        Payment.objects.create(participant=participant)
        saved, challenge = self.begin(participant_token=str(participant.token), next="https://attacker.invalid/")
        self.prove(saved, challenge)
        self.assertEqual(self.finish().url, reverse("game_account"))
        self.assertEqual(self.client.session["participant_return_token"], str(participant.token))
        participant.refresh_from_db()
        self.assertIsNone(participant.account_id)
        self.assertFalse(participant.registration_confirmed)
        identity = TelegramIdentity.objects.get(telegram_id=101)
        GameAccount.objects.create(user=identity.user, game_id="123", server_id="456", nickname="Player")
        self.client.logout()
        saved, challenge = self.begin(participant_token=str(participant.token))
        self.prove(saved, challenge)
        self.assertEqual(self.finish().url, reverse("participant", args=[participant.token]))

    def test_browser_status_is_private_and_contains_no_identity_before_approval(self):
        saved, challenge = self.begin()
        self.bot.handle_update(self.message("/start login_" + saved["nonce"]))
        response = self.client.get(reverse("telegram_auth_status"))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertIsNone(response.json()["telegram"])
        self.assertNotIn(saved["code"], str(self.api.call.call_args_list))
