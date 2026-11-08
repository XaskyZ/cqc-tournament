from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.models import Match, Outbox, Payment, PaymentAudit, Team
from arena.providers import ManualKaspiProvider
from arena.services import reopen_team, review_payment, submit_payment
from arena.team_services import join_team, remove_player, submit_team
from arena.tests.test_team_services import Fixtures


@override_settings(DEMO_MODE=False)
class TeamRecoveryTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create(username='reviewer', is_staff=True)
        self.current_team = self.team()
        self.player_user = self.user('player', '123')
        self.player = join_team(self.current_team, self.player_user)

    def test_draft_cannot_start_payment_even_by_direct_post(self):
        with self.assertRaises(ValidationError):
            submit_payment(self.player)
        self.player.refresh_from_db()
        self.assertIsNone(ManualKaspiProvider().create_checkout(self.player.payment))
        self.client.force_login(self.player_user)
        response = self.client.post(reverse('payment_submit', args=[self.player.token]))
        self.assertEqual(response.status_code, 302)
        self.player.payment.refresh_from_db()
        self.assertEqual(self.player.payment.status, Payment.Status.AWAITING)
        self.assertFalse(PaymentAudit.objects.exists())
        self.assertFalse(Outbox.objects.exists())
        submit_team(self.current_team, self.captain)
        self.assertEqual(submit_payment(self.player).status, Payment.Status.REVIEW)

    def test_reopen_keeps_paid_identity_and_audits_requires_new_submission(self):
        submit_team(self.current_team, self.captain)
        payment = submit_payment(self.player)
        review_payment(payment.pk, True, 'BANK-RECOVERY-1', actor_user=self.staff)
        original_payment = Payment.objects.values().get(pk=payment.pk)
        original_audits = list(PaymentAudit.objects.values())
        Team.objects.filter(pk=self.current_team.pk).update(
            status=Team.Status.REJECTED, rejection_reason='Old refusal',
            roster_checked_by=self.staff, roster_checked_at=timezone.now())
        team = reopen_team(self.current_team, self.staff, 'Evidence accepted')
        self.assertEqual(team.status, Team.Status.DRAFT)
        self.assertIsNone(team.submitted_at)
        self.assertIsNone(team.roster_checked_at)
        self.assertIsNone(team.roster_checked_by_id)
        self.assertEqual(Payment.objects.values().get(pk=payment.pk), original_payment)
        self.assertEqual(list(PaymentAudit.objects.values()), original_audits)
        log = LogEntry.objects.get()
        self.assertEqual(log.user_id, self.staff.pk)
        self.assertIn('Evidence accepted', log.change_message)
        self.assertIn('Old refusal', log.change_message)
        with self.assertRaises(ValidationError):
            remove_player(team, self.captain, self.player.pk)
        self.assertEqual(submit_team(team, self.captain).status, Team.Status.PENDING)

    def test_reopen_checks_actor_state_reason_and_bracket(self):
        with self.assertRaises(PermissionDenied):
            reopen_team(self.current_team, self.captain, 'Reason')
        with self.assertRaises(ValidationError):
            reopen_team(self.current_team, self.staff, 'Reason')
        Team.objects.filter(pk=self.current_team.pk).update(status=Team.Status.REJECTED)
        for reason in ('', 'x' * 1001):
            with self.assertRaises(ValidationError):
                reopen_team(self.current_team, self.staff, reason)
        Match.objects.create(tournament=self.tournament, key='W1', bracket='upper', round=1, index=1, best_of=1)
        with self.assertRaises(ValidationError):
            reopen_team(self.current_team, self.staff, 'Reason')
        self.current_team.refresh_from_db()
        self.assertEqual(self.current_team.status, Team.Status.REJECTED)
        self.assertFalse(LogEntry.objects.exists())

    def test_reopen_http_is_staff_only_post_and_keeps_payment(self):
        Team.objects.filter(pk=self.current_team.pk).update(status=Team.Status.REJECTED)
        url = reverse('team_reopen', args=[self.current_team.pk])
        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(url, {'reason': 'Not staff'}).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {'reason': 'Reviewed'}).status_code, 302)
        self.current_team.refresh_from_db()
        self.assertEqual(self.current_team.status, Team.Status.DRAFT)
        self.assertTrue(Payment.objects.filter(participant=self.player).exists())
