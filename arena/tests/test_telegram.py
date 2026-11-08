from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from arena.models import BotState, Outbox, Participant, Payment, PaymentAudit, Team, TelegramReviewSession, Tournament
from arena.telegram import CashierBot, TelegramClient, TelegramError


@override_settings(TELEGRAM_CASHIER_ID=855341070, TELEGRAM_BOT_TOKEN="test-secret")
class TelegramTests(TransactionTestCase):
    cashier = 855341070

    def setUp(self):
        captain = get_user_model().objects.create_user(username="captain")
        tournament = Tournament.objects.create()
        team = Team.objects.create(tournament=tournament, captain=captain, name="Team", contact="private contact")
        participant = Participant.objects.create(team=team, nickname="Player", game_id="secret-game", server_id="secret-server")
        self.payment = Payment.objects.create(participant=participant, status=Payment.Status.REVIEW, submitted_at=timezone.now())
        self.client = MagicMock()
        self.client.call.return_value = {"message_id": 100}
        self.bot = CashierBot(self.client)

    def session(self, action="approve", **kwargs):
        return TelegramReviewSession.objects.create(
            payment=self.payment, submitted_at_snapshot=self.payment.submitted_at,
            action=action, user_id=self.cashier, chat_id=self.cashier,
            expires_at=timezone.now() + timedelta(hours=1), **kwargs,
        )

    def callback(self, session, action="a", user=None, chat_type="private", chat_id=None):
        user = self.cashier if user is None else user
        return {"callback_query": {
            "id": "query", "from": {"id": user},
            "message": {"message_id": 50, "chat": {"id": user if chat_id is None else chat_id, "type": chat_type}},
            "data": f"pr:{action}:{session.pk.hex}",
        }}

    def message(self, text, user=None, reply=100, chat_type="private"):
        user = self.cashier if user is None else user
        message = {"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text}
        if reply is not None:
            message["reply_to_message"] = {"message_id": reply}
        return {"message": message}

    def assert_unreviewed(self):
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REVIEW)
        self.assertFalse(PaymentAudit.objects.exists())

    def test_wrong_sender_group_and_wrong_private_chat_are_denied(self):
        session = self.session(prompt_message_id=100, bank_reference="BANK-1")
        for update in [self.callback(session, "y", user=99), self.callback(session, "y", chat_type="group"), self.callback(session, "y", chat_id=99)]:
            self.bot.handle_update(update)
        self.assert_unreviewed()
        self.assertFalse(any(call.args[0] == "sendMessage" for call in self.client.call.call_args_list))

    def test_forged_callback_cannot_skip_bank_reference_or_change_action(self):
        session = self.session()
        self.bot.handle_update(self.callback(session, "r"))
        self.bot.handle_update(self.callback(session, "y"))
        update = self.callback(session)
        update["callback_query"]["data"] = "pr:y:not-a-uuid"
        self.bot.handle_update(update)
        self.assert_unreviewed()

    def test_reply_must_match_prompt_and_cashier(self):
        session = self.session(prompt_message_id=100)
        self.bot.handle_update(self.message("BANK-1", user=99))
        self.bot.handle_update(self.message("BANK-1", reply=999))
        self.bot.handle_update(self.message("BANK-1", chat_type="group"))
        session.refresh_from_db()
        self.assertEqual(session.bank_reference, "")
        self.assert_unreviewed()

    def test_expired_and_resubmitted_sessions_cannot_apply(self):
        session = self.session(prompt_message_id=100, bank_reference="BANK-1")
        TelegramReviewSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.bot.handle_update(self.callback(session, "y"))
        TelegramReviewSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() + timedelta(hours=1))
        Payment.objects.filter(pk=self.payment.pk).update(submitted_at=timezone.now() + timedelta(seconds=1))
        self.bot.handle_update(self.callback(session, "y"))
        self.bot.handle_update(self.message("BANK-1"))
        self.assert_unreviewed()

    def test_old_rejection_cannot_reject_a_new_submission(self):
        session = self.session(action="reject", prompt_message_id=100, reason="Old reason")
        Payment.objects.filter(pk=self.payment.pk).update(submitted_at=timezone.now() + timedelta(seconds=1))
        self.bot.handle_update(self.callback(session, "y"))
        self.assert_unreviewed()

    def test_duplicate_initial_click_does_not_create_another_prompt(self):
        session = self.session()
        self.bot.handle_update(self.callback(session))
        self.bot.handle_update(self.callback(session))
        sends = [call for call in self.client.call.call_args_list if call.args[0] == "sendMessage"]
        self.assertEqual(len(sends), 1)

    def test_approve_requires_reply_and_explicit_acknowledgment_once(self):
        session = self.session()
        self.bot.handle_update(self.callback(session))
        session.refresh_from_db()
        self.assertEqual(session.prompt_message_id, 100)
        self.bot.handle_update(self.message("bank-1"))
        self.assert_unreviewed()
        self.assertIn("Сверил Kaspi", str(self.client.call.call_args))
        # Constructing a fresh bot simulates process restart with persistent state.
        CashierBot(self.client).handle_update(self.callback(session, "y"))
        self.bot.handle_update(self.callback(session, "y"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(self.payment.bank_reference, "BANK-1")
        self.assertEqual(self.payment.reviewed_by_telegram, self.cashier)
        self.assertEqual(PaymentAudit.objects.count(), 1)
        self.assertEqual(Outbox.objects.filter(kind="result").count(), 1)
        session.refresh_from_db()
        self.assertIsNotNone(session.used_at)

    def test_reference_and_rejection_reason_are_immutable_before_final_click(self):
        session = self.session(action="reject", prompt_message_id=100)
        self.bot.handle_update(self.message("Поступление не найдено"))
        self.bot.handle_update(self.message("Другая причина"))
        session.refresh_from_db()
        self.assertEqual(session.reason, "Поступление не найдено")
        self.assert_unreviewed()
        self.bot.handle_update(self.callback(session, "y"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REJECTED)
        self.assertEqual(self.payment.rejection_reason, "Поступление не найдено")

    def test_participant_code_is_not_accepted_as_bank_reference(self):
        session = self.session(prompt_message_id=100)
        self.bot.handle_update(self.message(f"CQC-{self.payment.pk:06d}"))
        session.refresh_from_db()
        self.assertEqual(session.bank_reference, "")
        self.assert_unreviewed()

    def test_pending_and_identity_do_not_grant_roles(self):
        self.bot.handle_update(self.message("/pending", user=99, reply=None))
        self.assertNotIn("Team", str(self.client.call.call_args))
        self.bot.handle_update(self.message("/start", user=99, reply=None))
        self.assertIn("без права", self.client.call.call_args.kwargs["text"])
        self.bot.handle_update(self.message("/pending", reply=None))
        outgoing = str(self.client.call.call_args_list)
        self.assertNotIn("secret-game", outgoing)
        self.assertNotIn("secret-server", outgoing)
        self.assertNotIn("private contact", outgoing)
        self.assertEqual(TelegramReviewSession.objects.count(), 2)
        keyboard = self.client.call.call_args.kwargs["reply_markup"]["inline_keyboard"]
        for row in keyboard:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)

    def test_start_and_help_explain_sign_in_without_exposing_localhost(self):
        for command in ("/start", "/help"):
            self.bot.handle_update(self.message(command, user=99, reply=None))
            text = self.client.call.call_args.kwargs["text"]
            self.assertIn("Войти через Telegram", text)
            self.assertIn("по кнопке сайта", text)
            self.assertNotIn("localhost", text)
            self.assertNotIn("127.0.0.1", text)
            self.assertNotIn("/pending", text)
        self.bot.handle_update(self.message("/start", reply=None))
        self.assertIn("/pending", self.client.call.call_args.kwargs["text"])
        self.bot.handle_update(self.message("/pending", user=99, reply=None))
        self.assertIn("Доступ закрыт", self.client.call.call_args.kwargs["text"])

    def test_outbox_is_only_marked_sent_after_success_and_retries(self):
        item = Outbox.objects.create(payment=self.payment, kind="review")
        self.client.call.side_effect = TelegramError("sensitive transport details")
        self.bot.flush_outbox()
        item.refresh_from_db()
        self.assertIsNone(item.sent_at)
        self.assertEqual(item.attempts, 1)
        self.assertNotIn("sensitive", item.last_error)
        self.assertFalse(TelegramReviewSession.objects.exists())
        self.client.call.side_effect = None
        self.bot.flush_outbox()
        item.refresh_from_db()
        self.assertIsNotNone(item.sent_at)
        self.assertEqual(item.attempts, 2)
        self.assertEqual(item.last_error, "")

    def test_expired_unused_sessions_are_pruned_but_consumed_evidence_is_kept(self):
        expired = self.session()
        consumed = self.session(used_at=timezone.now())
        TelegramReviewSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.bot.flush_outbox()
        self.assertFalse(TelegramReviewSession.objects.filter(pk=expired.pk).exists())
        self.assertTrue(TelegramReviewSession.objects.filter(pk=consumed.pk).exists())

    def test_rejected_team_card_explains_payment_is_not_admission(self):
        team = self.payment.participant.team
        team.status = Team.Status.REJECTED
        team.save(update_fields=["status"])
        self.bot.send_review_card(self.payment)
        text = self.client.call.call_args.kwargs["text"]
        self.assertIn("Команда отклонена", text)
        self.assertIn("не даёт допуск", text)
        self.assertIn("вручную в Kaspi", text)

    def test_archived_payment_review_keeps_history_and_does_not_pay_replacement(self):
        from arena.roster_recovery import withdraw_player

        staff = get_user_model().objects.create_user(username="archiver", is_staff=True)
        original = self.payment.participant
        session = self.session(prompt_message_id=100, bank_reference="ARCHIVED-BANK-1")
        withdraw_player(original.pk, staff, "Игрок заменён до сверки банка", acknowledge=True)
        replacement = Participant.objects.create(
            team=original.team, nickname="Replacement", game_id="other-game", server_id="other-server",
        )
        replacement_payment = Payment.objects.create(participant=replacement)
        Outbox.objects.create(payment=self.payment, kind="review")
        self.bot.flush_outbox()
        self.assert_unreviewed()  # Delivery is never evidence of a bank payment.
        card = self.client.call.call_args.kwargs["text"]
        self.assertIn("Участник выведен из состава", card)
        self.assertIn("только ранее заявленное поступление", card)
        self.assertIn("не восстанавливает участие", card)
        self.assertIn("Новая заявка и её взнос оформляются отдельно", card)
        self.assertIn("вручную в Kaspi", card)

        before_review = timezone.now()
        self.bot.handle_update(self.callback(session, "y"))
        self.bot.handle_update(self.callback(session, "y"))
        self.payment.refresh_from_db()
        original.refresh_from_db()
        replacement_payment.refresh_from_db()
        original.team.refresh_from_db()
        self.assertFalse(original.active)
        self.assertEqual(original.withdrawn_by_id, staff.pk)
        self.assertEqual(original.team.status, Team.Status.DRAFT)
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(self.payment.participant_id, original.pk)
        self.assertEqual(self.payment.reviewed_by_telegram, self.cashier)
        self.assertIsNone(self.payment.reviewed_by_id)
        self.assertGreaterEqual(self.payment.reviewed_at, before_review)
        audit = PaymentAudit.objects.get(payment=self.payment)
        self.assertEqual((audit.from_status, audit.to_status), (Payment.Status.REVIEW, Payment.Status.CONFIRMED))
        self.assertEqual(audit.actor_telegram, self.cashier)
        self.assertIsNone(audit.actor_user_id)
        self.assertGreaterEqual(audit.created_at, before_review)
        self.assertEqual(audit.bank_reference, "ARCHIVED-BANK-1")
        self.assertEqual(replacement_payment.status, Payment.Status.AWAITING)
        self.assertEqual(replacement_payment.amount, 2200)
        self.assertIsNone(replacement_payment.bank_reference)
        self.assertEqual(Outbox.objects.filter(payment=self.payment, kind="result").count(), 1)
        snapshot = list(Payment.objects.order_by("pk").values())
        self.bot.flush_outbox()
        self.assertEqual(list(Payment.objects.order_by("pk").values()), snapshot)
        self.assertIn("Участник выведен из состава", self.client.call.call_args.kwargs["text"])

    def test_update_offset_persists_only_after_success(self):
        update = self.message("/start", reply=None)
        update["update_id"] = 31
        self.client.call.side_effect = [[update], TelegramError("failure")]
        with self.assertRaises(TelegramError):
            self.bot.poll_once()
        self.assertEqual(BotState.objects.get(pk="telegram_update_offset").value, "0")
        self.client.call.side_effect = [[update], {"message_id": 1}]
        self.bot.poll_once()
        self.assertEqual(BotState.objects.get(pk="telegram_update_offset").value, "32")
        self.client.call.side_effect = [[]]
        CashierBot(self.client).poll_once()
        self.assertEqual(self.client.call.call_args.kwargs["offset"], 32)

    def test_blocked_user_does_not_block_subsequent_cashier_updates(self):
        session = self.session(prompt_message_id=100, bank_reference="BANK-1")
        blocked = self.message("/start", user=99, reply=None)
        blocked["update_id"] = 50
        approve = self.callback(session, "y")
        approve["update_id"] = 51

        def api(method, **payload):
            if method == "getUpdates":
                return [blocked, approve]
            if method == "sendMessage" and payload["chat_id"] == 99:
                raise TelegramError("sendMessage: HTTP 403", method=method, status=403)
            return {"message_id": 101}

        self.client.call.side_effect = api
        self.bot.poll_once()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(BotState.objects.get(pk="telegram_update_offset").value, "52")

    def test_no_http_request_holds_database_transaction(self):
        def api(method, **payload):
            self.assertFalse(connection.in_atomic_block, f"{method} called inside transaction")
            return {"message_id": 100}

        self.client.call.side_effect = api
        session = self.session()
        self.bot.handle_update(self.callback(session))
        self.bot.handle_update(self.message("BANK-1"))
        self.bot.handle_update(self.callback(session, "y"))
        self.bot.flush_outbox()

    def test_confirmation_delivery_retries_immutable_saved_choice(self):
        session = self.session(prompt_message_id=100)
        self.client.call.side_effect = TelegramError("sendMessage: transport failure")
        with self.assertRaises(TelegramError):
            self.bot.handle_update(self.message("BANK-1"))
        session.refresh_from_db()
        self.assertEqual(session.bank_reference, "BANK-1")
        self.assert_unreviewed()
        self.client.call.side_effect = None
        CashierBot(self.client).handle_update(self.message("BANK-1"))
        self.assertIn("Сверил Kaspi", self.client.call.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["text"])
        self.bot.handle_update(self.callback(session, "y"))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)

    def test_prompt_delivery_retry_binds_only_successful_prompt(self):
        session = self.session()
        self.client.call.side_effect = TelegramError("sendMessage: transport failure")
        with self.assertRaises(TelegramError):
            self.bot.handle_update(self.callback(session))
        session.refresh_from_db()
        self.assertIsNone(session.prompt_message_id)
        self.client.call.side_effect = None
        self.bot.handle_update(self.callback(session))
        session.refresh_from_db()
        self.assertEqual(session.prompt_message_id, 100)

    def test_http_errors_never_expose_token(self):
        with patch("arena.telegram.urlopen", side_effect=URLError("https://api.telegram.org/bottest-secret/sendMessage")):
            with self.assertRaises(TelegramError) as caught:
                TelegramClient().call("sendMessage", chat_id=1, text="test")
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("https", str(caught.exception))
