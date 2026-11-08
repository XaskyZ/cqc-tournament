"""Captain revision preserves identity and financial history."""
from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from arena.models import Match, Payment, PaymentAudit, Team
from arena.team_services import (change_player_role, join_team, return_to_draft,
                                 rotate_invite, submit_team, update_team_details)
from arena.tests.test_team_services import Fixtures


class TeamRevisionTests(Fixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.current = self.team()
        self.member = self.user('member', '123')
        self.player = join_team(self.current, self.member)

    def test_return_edit_resubmit_preserves_payment_history(self):
        submit_team(self.current, self.captain)
        payment = self.player.payment
        payment.status = Payment.Status.CONFIRMED
        payment.bank_reference = 'REVISION-BANK'
        payment.reviewed_at = timezone.now()
        payment.save()
        PaymentAudit.objects.create(payment=payment, actor_user=self.other, from_status='review', to_status='confirmed', bank_reference='REVISION-BANK')
        Team.objects.filter(pk=self.current.pk).update(roster_checked_at=timezone.now(), roster_checked_by=self.other)
        before_payment = Payment.objects.values().get(pk=payment.pk)
        before_audits = list(PaymentAudit.objects.values())
        team = return_to_draft(self.current, self.captain, 'Correct contacts')
        self.assertEqual(team.status, 'draft')
        self.assertIsNone(team.submitted_at)
        self.assertIsNone(team.roster_checked_at)
        self.assertIsNone(team.roster_checked_by_id)
        update_team_details(team, self.captain, 'Renamed', '@new_captain', 'new.discord')
        self.assertEqual(submit_team(team, self.captain).status, 'pending')
        self.assertEqual(Payment.objects.values().get(pk=payment.pk), before_payment)
        self.assertEqual(list(PaymentAudit.objects.values()), before_audits)
        self.assertTrue(LogEntry.objects.filter(user=self.captain, change_message__contains='Correct contacts').exists())

    def test_return_only_pending_and_requires_reason(self):
        for state in ('draft', 'confirmed', 'rejected'):
            Team.objects.filter(pk=self.current.pk).update(status=state)
            with self.assertRaises(ValidationError):
                return_to_draft(self.current, self.captain, 'Reason')
        Team.objects.filter(pk=self.current.pk).update(status='pending')
        for reason in ('', 'x' * 1001, 'reason\ncontrol'):
            with self.assertRaises(ValidationError):
                return_to_draft(self.current, self.captain, reason)
        self.assertFalse(LogEntry.objects.exists())

    def test_mutations_are_captain_only(self):
        for actor in (self.member, self.other):
            operations = [
                lambda: return_to_draft(self.current, actor, 'Reason'),
                lambda: update_team_details(self.current, actor, 'Other', '@valid_name', 'discord'),
                lambda: rotate_invite(self.current, actor),
                lambda: change_player_role(self.current, actor, self.player.pk, 'reserve'),
            ]
            for operation in operations:
                with self.assertRaises(PermissionDenied):
                    operation()

    def test_deadlines_and_bracket_prevent_revision(self):
        for field in ('end_registration', 'starts_at'):
            original = getattr(self.tournament, field)
            setattr(self.tournament, field, timezone.now() - timedelta(seconds=1))
            type(self.tournament).objects.filter(pk=self.tournament.pk).update(**{field: getattr(self.tournament, field)})
            with self.assertRaises(ValidationError):
                rotate_invite(self.current, self.captain)
            Team.objects.filter(pk=self.current.pk).update(status='pending')
            with self.assertRaises(ValidationError):
                return_to_draft(self.current, self.captain, 'Reason')
            Team.objects.filter(pk=self.current.pk).update(status='draft')
            setattr(self.tournament, field, original)
            type(self.tournament).objects.filter(pk=self.tournament.pk).update(**{field: original})
        Match.objects.create(tournament=self.tournament, key='REV-1', bracket='upper', round=1, index=1, best_of=3)
        with self.assertRaises(ValidationError):
            rotate_invite(self.current, self.captain)

    def test_role_updates_only_untouched_fee_and_respects_slots(self):
        payment_pk = self.player.payment.pk
        token = self.player.token
        change_player_role(self.current, self.captain, self.player.pk, 'reserve')
        self.player.refresh_from_db()
        self.player.payment.refresh_from_db()
        self.assertTrue(self.player.is_reserve)
        self.assertEqual(self.player.payment.amount, 0)
        self.assertFalse(self.player.payment.required)
        self.assertEqual(self.player.payment.pk, payment_pk)
        self.assertEqual(self.player.token, token)
        new_member = self.user('second', '234')
        join_team(self.current, new_member)
        with self.assertRaises(ValidationError):
            change_player_role(self.current, self.captain, self.player.pk, 'main')
        with self.assertRaises(ValidationError):
            change_player_role(self.current, self.captain, self.player.pk, 'invalid')

    def test_role_main_restores_required_fee(self):
        change_player_role(self.current, self.captain, self.player.pk, 'reserve')
        change_player_role(self.current, self.captain, self.player.pk, 'main')
        self.player.payment.refresh_from_db()
        self.assertEqual(self.player.payment.amount, 2200)
        self.assertTrue(self.player.payment.required)

    def test_role_cannot_modify_archived_or_other_team_participant(self):
        other_team = self.team(self.other, 'Other')
        outsider = self.user('outsider', '345')
        other_player = join_team(other_team, outsider)
        with self.assertRaises(ValidationError):
            change_player_role(self.current, self.captain, other_player.pk, 'reserve')
        self.player.active = False
        self.player.save(update_fields=['active'])
        with self.assertRaises(ValidationError):
            change_player_role(self.current, self.captain, self.player.pk, 'reserve')

    def test_role_blocks_all_financial_traces_even_awaiting(self):
        payment = self.player.payment
        cases = {'submitted_at': timezone.now(), 'reviewed_at': timezone.now(),
                 'bank_reference': 'BANK-TRACE', 'provider_payment_id': 'provider-trace',
                 'reviewed_by_id': self.other.pk, 'reviewed_by_telegram': 123,
                 'rejection_reason': 'Prior review', 'status': 'review'}
        for field, value in cases.items():
            previous = getattr(payment, field)
            Payment.objects.filter(pk=payment.pk).update(**{field: value})
            with self.assertRaises(ValidationError):
                change_player_role(self.current, self.captain, self.player.pk, 'reserve')
            Payment.objects.filter(pk=payment.pk).update(**{field: previous})
        PaymentAudit.objects.create(payment=payment, from_status='awaiting', to_status='review')
        with self.assertRaises(ValidationError):
            change_player_role(self.current, self.captain, self.player.pk, 'reserve')
        self.player.refresh_from_db()
        self.assertFalse(self.player.is_reserve)

    def test_details_validate_contacts_name_and_other_team(self):
        update_team_details(self.current, self.captain, 'First', '@valid_name', 'discord')
        self.team(self.other, 'Taken')
        for name, tg, discord in [('taken', '@valid_name', 'discord'), ('Changed', '+77001112233', 'discord'), ('Changed', '@valid_name', 'x@example.com')]:
            with self.assertRaises(ValidationError):
                update_team_details(self.current, self.captain, name, tg, discord)
        submit_team(self.current, self.captain)
        with self.assertRaises(ValidationError):
            update_team_details(self.current, self.captain, 'Changed', '@valid_name', 'discord')

    def test_rotation_old_invite_404_new_keeps_roster(self):
        previous = self.current.invite_token
        current = rotate_invite(self.current, self.captain)
        self.assertNotEqual(previous, current.invite_token)
        self.assertEqual(self.client.get(reverse('team_join', args=[previous])).status_code, 404)
        self.assertEqual(self.client.get(reverse('team_join', args=[current.invite_token])).status_code, 200)
        self.assertEqual(current.participants.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_http_post_csrf_and_access_enforced(self):
        urls = [reverse('team_return_to_draft', args=[self.current.pk]),
                reverse('team_update_details', args=[self.current.pk]),
                reverse('team_rotate_invite', args=[self.current.pk]),
                reverse('team_change_role', args=[self.current.pk, self.player.pk])]
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.captain)
        for url in urls:
            self.assertEqual(csrf_client.get(url).status_code, 405)
            self.assertEqual(csrf_client.post(url, {'reason': 'Reason', 'role': 'reserve'}).status_code, 403)
        self.client.force_login(self.other)
        for url in urls:
            self.assertEqual(self.client.post(url, {'reason': 'Reason', 'role': 'reserve'}).status_code, 403)

    def test_http_return_edit_resubmit(self):
        submit_team(self.current, self.captain)
        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(reverse('team_return_to_draft', args=[self.current.pk]), {'reason': 'Update contacts'}).status_code, 302)
        response = self.client.post(reverse('team_update_details', args=[self.current.pk]), {'name': 'New name', 'telegram_contact': '@new_captain', 'discord_contact': 'new.discord'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(reverse('team_submit', args=[self.current.pk])).status_code, 302)
        self.current.refresh_from_db()
        self.assertEqual(self.current.status, 'pending')
        self.assertEqual(self.current.name, 'New name')
