from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.game_accounts import claim_participant, save_profile
from arena.game_lookup import LookupResult
from arena.models import GameAccount, Match, Outbox, Participant, Payment, PaymentAudit, Team, TelegramIdentity, Tournament
from arena.providers import ManualKaspiProvider
from arena.roster_recovery import withdraw_player
from arena.services import check_roster, confirm_team, review_payment, submit_payment, team_readiness
from arena.team_services import change_player_role, create_team, join_team, submission_errors, submit_team
from arena.team_views import user_team


@override_settings(MLBB_LOOKUP_PROVIDER="gopay", DEMO_MODE=False)
class RosterRecoveryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = get_user_model().objects.create(username="staff", is_staff=True)
        self.captain = self.user("captain")
        self.old_user = self.user("old-player", "123")
        self.new_user = self.user("new-player", "456")
        starts = timezone.now() + timedelta(days=3)
        self.tournament = Tournament.objects.create(
            rules="Test rules", terms_confirmed=True, registration_open=True,
            starts_at=starts, end_registration=starts-timedelta(hours=1), event_date=starts.date(),
            timezone_name="UTC", max_teams=4, roster_size=1, reserve_limit=1,
            reserves_pay=False, best_of=3, final_best_of=5, grand_final_reset=True,
        )
        self.team = create_team(self.tournament, self.captain, "Original team", "@captain_name", "discord")
        self.old = join_team(self.team, self.old_user)
        submit_team(self.team, self.captain)
        payment = submit_payment(self.old)
        review_payment(payment.pk, True, bank_reference="OLD-BANK-PAYMENT", actor_user=self.staff)
        check_roster(self.team, self.staff)
        confirm_team(self.team, self.staff)
        self.team.refresh_from_db()

    def user(self, name, game_id=None):
        user = get_user_model().objects.create(username=name)
        TelegramIdentity.objects.create(user=user, telegram_id=10000+user.pk)
        if game_id:
            GameAccount.objects.create(user=user, game_id=game_id, server_id="45", nickname=name,
                                       lookup_provider="gopay", lookup_checked_at=timezone.now())
        return user

    def finance(self, player):
        payment_id = Payment.objects.get(participant=player).pk
        return {
            "payment": Payment.objects.filter(pk=payment_id).values().get(),
            "audits": list(PaymentAudit.objects.filter(payment_id=payment_id).order_by("pk").values()),
            "notifications": list(Outbox.objects.filter(payment_id=payment_id).order_by("pk").values()),
        }

    def withdraw(self):
        return withdraw_player(self.old.pk, self.staff, "Игрок попросил замену", acknowledge=True)

    def test_paid_history_stays_intact_and_replacement_requires_its_own_2200(self):
        before = self.finance(self.old)
        original_token = self.old.token
        archived = self.withdraw()
        self.assertFalse(archived.active)
        self.assertEqual(archived.withdrawn_by, self.staff)
        self.assertIsNotNone(archived.withdrawn_at)
        self.assertEqual(archived.withdrawal_reason, "Игрок попросил замену")
        self.assertEqual(archived.token, original_token)
        self.assertEqual(self.finance(self.old), before)
        self.assertTrue(LogEntry.objects.filter(user=self.staff, object_id=str(self.old.pk), change_message__contains="Игрок попросил замену").exists())
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)
        self.assertIsNone(self.team.submitted_at)
        self.assertIsNone(self.team.roster_checked_at)
        self.assertIsNone(self.team.roster_checked_by_id)
        self.assertTrue(submission_errors(self.team))
        replacement = join_team(self.team, self.new_user)
        self.assertNotEqual(replacement.pk, self.old.pk)
        self.assertNotEqual(replacement.token, original_token)
        self.assertEqual(replacement.payment.amount, 2200)
        self.assertTrue(replacement.payment.required)
        self.assertEqual(replacement.payment.status, Payment.Status.AWAITING)
        self.assertIsNone(replacement.payment.bank_reference)
        self.assertEqual(self.team.participants.count(), 2)
        self.assertEqual(self.team.participants.filter(active=True).count(), 1)
        self.assertEqual(submission_errors(self.team), [])
        submit_team(self.team, self.captain)
        check_roster(self.team, self.staff)
        self.team.refresh_from_db()
        with self.assertRaises(ValidationError):
            confirm_team(self.team, self.staff)
        new_payment = submit_payment(replacement)
        with self.assertRaises(ValidationError):
            review_payment(new_payment.pk, True, bank_reference="OLD-BANK-PAYMENT", actor_user=self.staff)
        review_payment(new_payment.pk, True, bank_reference="NEW-BANK-PAYMENT", actor_user=self.staff)
        self.assertEqual(team_readiness(self.team), [])
        self.assertEqual(confirm_team(self.team, self.staff).status, Team.Status.CONFIRMED)
        self.assertEqual(self.finance(self.old), before)
        public = self.client.get(reverse("teams"))
        self.assertContains(public, "new-player")
        self.assertNotContains(public, "old-player")
        self.assertNotContains(public, str(self.old.token))

    def test_archived_unconfirmed_roster_and_unpaid_payment_do_not_block_admission(self):
        # A second withdrawn row was never paid or personally confirmed.
        archived = Participant.objects.create(team=self.team, game_id="999", server_id="45", nickname="Unclaimed history", active=False)
        Payment.objects.create(participant=archived)
        self.assertEqual(submission_errors(self.team), [])
        self.assertEqual(team_readiness(self.team), [])
        self.assertEqual(confirm_team(self.team, self.staff).status, Team.Status.CONFIRMED)

    def test_same_id_can_rejoin_as_fresh_row_without_reusing_old_fee(self):
        before = self.finance(self.old)
        self.withdraw()
        fresh = join_team(self.team, self.old_user)
        self.assertNotEqual(fresh.pk, self.old.pk)
        self.assertNotEqual(fresh.token, self.old.token)
        self.assertEqual((fresh.game_id, fresh.server_id), (self.old.game_id, self.old.server_id))
        self.assertEqual(fresh.payment.status, Payment.Status.AWAITING)
        self.assertEqual(fresh.payment.amount, 2200)
        self.assertEqual(self.finance(self.old), before)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Participant.objects.create(team=self.team, game_id=fresh.game_id, server_id=fresh.server_id, nickname="Duplicate")

    def test_own_profile_becomes_editable_but_archived_identity_snapshot_does_not_change(self):
        before = self.finance(self.old)
        self.withdraw()
        self.assertIsNone(user_team(self.old_user, self.tournament))
        self.client.force_login(self.old_user)
        response = self.client.get(reverse("game_account"))
        self.assertFalse(response.context["profile_locked"])
        with patch("arena.game_lookup.lookup", return_value=LookupResult("New profile", "gopay", "789", "45")):
            profile = save_profile(self.old_user, {"game_id": "789", "server_id": "45", "accept_identity": True})
        self.assertEqual(profile.game_id, "789")
        self.old.refresh_from_db()
        self.assertEqual((self.old.game_id, self.old.nickname), ("123", "old-player"))
        self.assertEqual(self.finance(self.old), before)
        own_team = create_team(self.tournament, self.old_user, "New own team", "@old_player", "old_discord")
        self.assertEqual(own_team.captain, self.old_user)

    def test_old_token_cannot_claim_submit_payment_or_create_checkout(self):
        before = self.finance(self.old)
        archived = self.withdraw()
        with self.assertRaises(ValidationError):
            claim_participant(self.old_user, archived.token)
        with self.assertRaises(ValidationError):
            submit_payment(archived)
        self.assertIsNone(ManualKaspiProvider().create_checkout(Payment.objects.get(participant=archived)))
        self.client.force_login(self.old_user)
        page = self.client.get(reverse("participant", args=[archived.token]))
        self.assertContains(page, "Участие завершено")
        self.assertNotContains(page, reverse("participant_claim", args=[archived.token]))
        self.assertNotContains(page, reverse("payment_submit", args=[archived.token]))
        self.assertNotContains(page, "https://pay.kaspi.kz/")
        for name, data in (("participant_confirm", {"accept_identity": "on"}), ("participant_claim", {"accept_identity": "on"}), ("payment_submit", {})):
            response = self.client.post(reverse(name, args=[archived.token]), data)
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.finance(self.old), before)
        archived.refresh_from_db()
        self.assertFalse(archived.active)
        self.client.force_login(self.new_user)
        self.assertEqual(self.client.post(reverse("payment_submit", args=[archived.token])).status_code, 403)

    def test_role_change_counts_only_active_slots_and_preserves_archived_fee(self):
        before = self.finance(self.old)
        self.withdraw()
        reserve = join_team(self.team, self.new_user, role="reserve")
        self.assertEqual(reserve.payment.amount, 0)
        promoted = change_player_role(self.team, self.captain, reserve.pk, "main")
        self.assertFalse(promoted.is_reserve)
        promoted.payment.refresh_from_db()
        self.assertEqual((promoted.payment.amount, promoted.payment.required), (2200, True))
        self.assertEqual(self.finance(self.old), before)
        with self.assertRaises(ValidationError):
            change_player_role(self.team, self.captain, self.old.pk, "reserve")

    def test_existing_review_can_be_reconciled_after_archive_without_reactivating_player(self):
        self.withdraw()
        replacement = join_team(self.team, self.new_user)
        submit_team(self.team, self.captain)
        payment = submit_payment(replacement)
        withdraw_player(replacement.pk, self.staff, "Заявлен неверный аккаунт", acknowledge=True)
        reviewed = review_payment(payment.pk, True, bank_reference="RECEIVED-BEFORE-ARCHIVE", actor_user=self.staff)
        self.assertEqual(reviewed.status, Payment.Status.CONFIRMED)
        self.assertEqual(reviewed.participant_id, replacement.pk)
        replacement.refresh_from_db()
        self.assertFalse(replacement.active)
        self.assertEqual(PaymentAudit.objects.filter(payment=reviewed).count(), 2)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)
        self.assertTrue(submission_errors(self.team))

    def test_archive_is_visible_only_in_own_or_captain_history_not_public_roster(self):
        self.withdraw()
        fresh = join_team(self.team, self.new_user)
        public = self.client.get(reverse("team_join", args=[self.team.invite_token]))
        self.assertNotContains(public, "old-player")
        self.assertNotContains(public, str(self.old.token))
        self.assertNotContains(public, "OLD-BANK-PAYMENT")
        self.client.force_login(self.new_user)
        member = self.client.get(reverse("captain"))
        self.assertEqual(list(member.context["archived_own"]), [])
        self.assertEqual(member.context["archived_participants"], [])
        self.assertNotContains(member, str(self.old.token))
        self.client.force_login(self.old_user)
        old_dashboard = self.client.get(reverse("captain"))
        self.assertIsNone(old_dashboard.context["team"])
        self.assertEqual([p.pk for p in old_dashboard.context["archived_own"]], [self.old.pk])
        self.assertContains(old_dashboard, reverse("participant", args=[self.old.token]))
        self.client.force_login(self.captain)
        captain_dashboard = self.client.get(reverse("captain"))
        self.assertEqual([p.pk for p in captain_dashboard.context["participants"]], [fresh.pk])
        self.assertEqual([p.pk for p in captain_dashboard.context["archived_participants"]], [self.old.pk])

    def test_only_active_staff_with_reason_and_acknowledgement_can_archive(self):
        inactive_staff = get_user_model().objects.create(username="inactive", is_staff=True, is_active=False)
        for actor in (self.captain, self.old_user, self.new_user, AnonymousUser(), inactive_staff):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                withdraw_player(self.old.pk, actor, "Reason", acknowledge=True)
        for reason, ack in (("", True), ("x"*1001, True), ("Reason", False), ("Reason", "true")):
            with self.subTest(reason=reason[:10], ack=ack), self.assertRaises(ValidationError):
                withdraw_player(self.old.pk, self.staff, reason, acknowledge=ack)
        self.old.refresh_from_db()
        self.assertTrue(self.old.active)
        self.assertFalse(LogEntry.objects.filter(object_id=str(self.old.pk), change_message__contains="Вывод из состава").exists())

    def test_cutoff_start_or_created_bracket_blocks_archive(self):
        for field in ("end_registration", "starts_at"):
            original = getattr(self.tournament, field)
            Tournament.objects.filter(pk=self.tournament.pk).update(**{field: timezone.now()-timedelta(seconds=1)})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.withdraw()
            Tournament.objects.filter(pk=self.tournament.pk).update(**{field: original})
        Match.objects.create(tournament=self.tournament, key="W1-1", bracket="upper", round=1, index=1, best_of=3)
        with self.assertRaises(ValidationError):
            self.withdraw()
        self.old.refresh_from_db()
        self.assertTrue(self.old.active)

    def test_rejected_status_stays_rejected_and_repeat_archive_cannot_change_audit(self):
        Team.objects.filter(pk=self.team.pk).update(status=Team.Status.REJECTED)
        self.withdraw()
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.REJECTED)
        logs = LogEntry.objects.count()
        with self.assertRaises(ValidationError):
            self.withdraw()
        self.assertEqual(LogEntry.objects.count(), logs)

    def test_withdraw_web_requires_staff_post_csrf_and_explicit_consent(self):
        url = reverse("withdraw_player", args=[self.old.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(url, {"reason": "Reason", "acknowledge": "on"}).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.old.refresh_from_db()
        self.assertTrue(self.old.active)
        missing_ack = self.client.post(url, {"reason": "Reason"})
        self.assertEqual(missing_ack.status_code, 200)
        self.old.refresh_from_db()
        self.assertTrue(self.old.active)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        self.assertEqual(csrf_client.post(url, {"reason": "Reason", "acknowledge": "on"}).status_code, 403)
        response = self.client.post(url, {"reason": "Reason", "acknowledge": "on"})
        self.assertRedirects(response, reverse("organizer"), fetch_redirect_response=False)
        self.old.refresh_from_db()
        self.assertFalse(self.old.active)
