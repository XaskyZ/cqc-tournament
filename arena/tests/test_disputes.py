"""Dispute recovery changes profiles only; all external lookup calls are mocks."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.account_disputes import review_dispute, submit_dispute
from arena.game_lookup import LookupResult, LookupUnavailable
from arena.models import GameAccount, GameAccountDispute, Outbox, Participant, Payment, PaymentAudit, Team, TelegramIdentity, Tournament


@override_settings(DEMO_MODE=False, MLBB_LOOKUP_PROVIDER='gopay', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AccountDisputeTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        users = get_user_model()
        self.claimant = users.objects.create_user('claimant')
        self.owner = users.objects.create_user('PRIVATE_CURRENT_OWNER', email='private-owner@example.invalid')
        self.other = users.objects.create_user('other_claimant')
        self.staff = users.objects.create_user('staff', is_staff=True)
        for i, user in enumerate((self.claimant, self.owner, self.other), 1):
            TelegramIdentity.objects.create(user=user, telegram_id=8000 + i)
        self.source = GameAccount.objects.create(user=self.owner, game_id='123456789', server_id='4321', nickname='Old cached nickname', lookup_provider='gopay', lookup_checked_at=timezone.now())
        self.lookup_result = LookupResult('Nickname from provider', 'gopay', '123456789', '4321')
        blocker = patch('arena.game_lookup._open_request', side_effect=AssertionError('Real lookup forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def dispute(self, claimant=None):
        with patch('arena.account_disputes.game_lookup.lookup', return_value=self.lookup_result):
            return submit_dispute(claimant or self.claimant, '123456789', '4321', 'Please verify this account in the game.')

    def review(self, dispute, approve=True, **changes):
        payload = {'approve': approve, 'reason': 'Situation checked in game; decision explained.', 'checked_in_game': True, 'actor': self.staff}
        payload.update(changes)
        return review_dispute(dispute.pk, **payload)

    def participant(self, user, active=True, paid=False):
        tournament = Tournament.objects.first() or Tournament.objects.create()
        team = Team.objects.create(tournament=tournament, captain=user, name=f'Team {user.pk}', contact='private')
        profile = GameAccount.objects.get(user=user)
        player = Participant.objects.create(team=team, account=user, game_id=profile.game_id, server_id=profile.server_id, nickname=profile.nickname,
                                            active=active, registration_confirmed=True,
                                            withdrawn_at=timezone.now() if not active else None,
                                            withdrawn_by=self.staff if not active else None,
                                            withdrawal_reason='Explicit archival preserving finance' if not active else '')
        payment = Payment.objects.create(participant=player, status=Payment.Status.CONFIRMED if paid else Payment.Status.AWAITING,
                                         bank_reference='AUDIT-BANK-1' if paid else None,
                                         reviewed_by=self.staff if paid else None,
                                         reviewed_at=timezone.now() if paid else None)
        if paid:
            PaymentAudit.objects.create(payment=payment, actor_user=self.staff, from_status='review', to_status='confirmed', bank_reference='AUDIT-BANK-1')
            Outbox.objects.create(payment=payment, kind='result')
        return player

    def test_submission_uses_fresh_lookup_outside_transaction_and_does_not_move_profile(self):
        def lookup(game_id, server_id):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual((game_id, server_id), ('123456789', '4321'))
            return self.lookup_result

        with patch('arena.account_disputes.game_lookup.lookup', side_effect=lookup):
            dispute = submit_dispute(self.claimant, '00123456789', '04321', 'Please check the account in game.')
        self.assertEqual(dispute.nickname, self.lookup_result.nickname)
        self.assertEqual(dispute.old_owner_id, self.owner.pk)
        self.assertEqual(dispute.old_account_pk, self.source.pk)
        self.assertEqual(dispute.status, GameAccountDispute.Status.PENDING)
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk, user=self.owner).exists())
        self.assertFalse(GameAccount.objects.filter(user=self.claimant).exists())

    def test_failed_lookup_and_wrong_pair_create_nothing(self):
        for outcome in (LookupUnavailable('Unavailable'), LookupResult('Wrong pair', 'gopay', '111', '4321')):
            with patch('arena.account_disputes.game_lookup.lookup', **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                with self.assertRaises((LookupUnavailable, ValidationError)):
                    submit_dispute(self.claimant, '123456789', '4321', 'Check this account')
        self.assertFalse(GameAccountDispute.objects.exists())
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk).exists())

    def test_duplicate_pending_dispute_is_rejected_before_lookup(self):
        self.dispute()
        with patch('arena.account_disputes.game_lookup.lookup') as lookup:
            with self.assertRaises(ValidationError):
                submit_dispute(self.claimant, '123456789', '4321', 'Another request')
            lookup.assert_not_called()
        self.assertEqual(GameAccountDispute.objects.count(), 1)

    def test_submission_requires_active_telegram_and_conflicting_pair(self):
        TelegramIdentity.objects.filter(user=self.other).delete()
        with self.assertRaises(PermissionDenied):
            self.dispute(self.other)
        get_user_model().objects.filter(pk=self.claimant.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.dispute()
        with self.assertRaises(ValidationError), patch('arena.account_disputes.game_lookup.lookup') as lookup:
            submit_dispute(self.owner, '123456789', '4321', 'Already mine')
        lookup.assert_not_called()

    def test_approval_replaces_unused_claimant_profile_and_keeps_audit_snapshot(self):
        previous = GameAccount.objects.create(user=self.claimant, game_id='987654321', server_id='1234', nickname='Previous claimant profile')
        dispute = self.dispute()
        decided = self.review(dispute)
        profile = GameAccount.objects.get(user=self.claimant)
        self.assertEqual(profile.pk, previous.pk)
        self.assertEqual((profile.game_id, profile.server_id, profile.nickname), ('123456789', '4321', self.lookup_result.nickname))
        self.assertFalse(GameAccount.objects.filter(user=self.owner).exists())
        self.assertEqual(decided.claimant_previous_game_id, '987654321')
        self.assertEqual(decided.old_owner_id, self.owner.pk)
        self.assertEqual(decided.reviewed_by_id, self.staff.pk)
        self.assertIsNotNone(decided.reviewed_at)
        self.assertTrue(decided.checked_in_game)

    def test_approval_for_first_profile_uses_lookup_snapshot_without_new_network(self):
        dispute = self.dispute()
        with patch('arena.account_disputes.game_lookup.lookup', side_effect=AssertionError('Review must use saved lookup')):
            self.review(dispute)
        profile = GameAccount.objects.get(user=self.claimant)
        self.assertEqual(profile.nickname, self.lookup_result.nickname)
        self.assertEqual(profile.lookup_checked_at, dispute.lookup_checked_at)

    def test_active_owner_or_claimant_membership_blocks_approval_but_allows_rejection(self):
        self.participant(self.owner)
        dispute = self.dispute()
        with self.assertRaises(ValidationError):
            self.review(dispute)
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk).exists())
        self.review(dispute, approve=False)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, GameAccountDispute.Status.REJECTED)
        self.assertFalse(GameAccount.objects.filter(user=self.claimant).exists())

    def test_active_claimant_membership_blocks_profile_overwrite(self):
        previous = GameAccount.objects.create(user=self.claimant, game_id='987654321', server_id='1234', nickname='Claimant')
        self.participant(self.claimant)
        dispute = self.dispute()
        with self.assertRaises(ValidationError):
            self.review(dispute)
        previous.refresh_from_db()
        self.assertEqual(previous.game_id, '987654321')
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk).exists())

    def test_archived_paid_membership_remains_with_previous_owner_and_payment(self):
        player = self.participant(self.owner, active=False, paid=True)
        payment = player.payment
        player_snapshot = Participant.objects.filter(pk=player.pk).values().get()
        payment_snapshot = Payment.objects.filter(pk=payment.pk).values().get()
        audits = list(PaymentAudit.objects.filter(payment=payment).values())
        outbox = list(Outbox.objects.filter(payment=payment).values())
        dispute = self.dispute()
        self.review(dispute)
        self.assertEqual(Participant.objects.filter(pk=player.pk).values().get(), player_snapshot)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), payment_snapshot)
        self.assertEqual(list(PaymentAudit.objects.filter(payment=payment).values()), audits)
        self.assertEqual(list(Outbox.objects.filter(payment=payment).values()), outbox)
        self.assertEqual(GameAccount.objects.get(user=self.claimant).game_id, player.game_id)
        self.assertEqual(Participant.objects.get(pk=player.pk).account_id, self.owner.pk)

    def test_changed_source_or_claimant_profile_requires_a_new_dispute(self):
        dispute = self.dispute()
        self.source.nickname = 'Changed after request'
        self.source.save()
        with self.assertRaises(ValidationError):
            self.review(dispute)
        self.review(dispute, approve=False)
        fresh = self.dispute()
        GameAccount.objects.create(user=self.claimant, game_id='999999', server_id='321', nickname='Newly selected')
        with self.assertRaises(ValidationError):
            self.review(fresh)
        self.assertEqual(GameAccount.objects.get(user=self.claimant).game_id, '999999')

    def test_decision_requires_staff_reason_checkbox_and_cannot_repeat_or_mutate(self):
        dispute = self.dispute()
        with self.assertRaises(PermissionDenied):
            self.review(dispute, actor=self.claimant)
        for values in ({'reason': ''}, {'checked_in_game': False}, {'approve': 'yes'}):
            with self.assertRaises(ValidationError):
                self.review(dispute, **values)
        self.review(dispute, approve=False)
        with self.assertRaises(ValidationError):
            self.review(dispute)
        dispute.refresh_from_db()
        dispute.decision_reason = 'Rewrite'
        with self.assertRaises(ValidationError):
            dispute.save()
        with self.assertRaises(ValidationError):
            GameAccountDispute.objects.filter(pk=dispute.pk).update(status='approved')
        with self.assertRaises(ValidationError):
            dispute.delete()

    def test_claimant_losing_telegram_or_being_disabled_cannot_receive_profile(self):
        dispute = self.dispute()
        TelegramIdentity.objects.filter(user=self.claimant).delete()
        with self.assertRaises(PermissionDenied):
            self.review(dispute)
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk).exists())
        TelegramIdentity.objects.create(user=self.claimant, telegram_id=9001)
        get_user_model().objects.filter(pk=self.claimant.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.review(dispute)

    def test_failed_profile_validation_rolls_back_old_profile_deletion_and_decision(self):
        dispute = self.dispute()
        with patch.object(GameAccount, 'full_clean', side_effect=ValidationError('Test invalid profile')):
            with self.assertRaises(ValidationError):
                self.review(dispute)
        self.assertTrue(GameAccount.objects.filter(pk=self.source.pk, user=self.owner).exists())
        self.assertFalse(GameAccount.objects.filter(user=self.claimant).exists())
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, GameAccountDispute.Status.PENDING)

    def test_private_http_submission_has_no_manual_nickname_or_current_owner_leak(self):
        self.client.force_login(self.claimant)
        with patch('arena.account_disputes.game_lookup.lookup', return_value=self.lookup_result):
            response = self.client.post(reverse('account_disputes'), {'game_id': '123456789', 'server_id': '4321', 'explanation': 'Please check the game account', 'nickname': 'Forged nickname', 'old_owner': self.claimant.pk})
        self.assertRedirects(response, reverse('account_disputes'), fetch_redirect_response=False)
        dispute = GameAccountDispute.objects.get()
        self.assertEqual(dispute.nickname, self.lookup_result.nickname)
        self.assertEqual(dispute.old_owner_id, self.owner.pk)
        response = self.client.get(reverse('account_disputes'))
        self.assertContains(response, self.lookup_result.nickname)
        self.assertNotContains(response, self.owner.username)
        self.assertNotContains(response, self.owner.email)
        self.assertNotContains(response, 'name="nickname"')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertNotIn('old_owner', response.context['disputes'][0])
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse('account_disputes')), self.lookup_result.nickname)

    def test_staff_pages_are_private_and_form_requires_checkbox_and_reason(self):
        dispute = self.dispute()
        review_url = reverse('account_dispute_review', args=[dispute.pk])
        self.assertEqual(self.client.get(reverse('account_dispute_queue')).status_code, 302)
        self.client.force_login(self.claimant)
        self.assertEqual(self.client.get(review_url).status_code, 403)
        self.assertEqual(self.client.get(reverse('account_dispute_queue')).status_code, 403)
        self.client.force_login(self.staff)
        response = self.client.get(review_url)
        self.assertContains(response, self.owner.username)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.client.post(review_url, {'decision': 'approve', 'reason': 'Checked in game'}).status_code, 200)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, GameAccountDispute.Status.PENDING)
        self.assertEqual(self.client.post(review_url, {'decision': 'approve', 'reason': 'Checked in game', 'checked_in_game': 'on'}).status_code, 302)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, GameAccountDispute.Status.APPROVED)

    def test_csrf_and_guest_access_do_not_create_or_decide_disputes(self):
        self.assertEqual(self.client.post(reverse('account_disputes'), {}).status_code, 302)
        dispute = self.dispute()
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.claimant)
        self.assertEqual(browser.post(reverse('account_disputes'), {'game_id': '123456789', 'server_id': '4321', 'explanation': 'Check'}).status_code, 403)
        browser.force_login(self.staff)
        self.assertEqual(browser.post(reverse('account_dispute_review', args=[dispute.pk]), {'decision': 'approve', 'reason': 'Checked', 'checked_in_game': 'on'}).status_code, 403)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, GameAccountDispute.Status.PENDING)
