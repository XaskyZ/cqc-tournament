from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from arena.models import GameAccount, RulesAcceptance, Team, TelegramIdentity, Tournament
from arena.rules import accept_rules, has_accepted, rules_version


class RulesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('rules-player')
        TelegramIdentity.objects.create(user=self.user, telegram_id=345678)
        GameAccount.objects.create(user=self.user, nickname='Player', game_id='12345', server_id='123')
        self.tournament = Tournament.objects.create(rules='Тестовый регламент', reserve_limit=0, reserves_pay=False)
        self.team = Team.objects.create(tournament=self.tournament, captain=self.user, name='Rules', status='draft')
        self.client.force_login(self.user)

    def test_version_snapshot_idempotence_and_immutability(self):
        version = rules_version(self.tournament)
        first = accept_rules(self.tournament, self.user, version, True)
        second = accept_rules(self.tournament, self.user, version, True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.snapshot['rules'], 'Тестовый регламент')
        self.assertEqual(first.snapshot['participant_fee_kzt'], 2200)
        self.assertTrue(first.accepted_at)
        with self.assertRaises(ValidationError):
            RulesAcceptance.objects.filter(pk=first.pk).update(version='changed')
        with self.assertRaises(ValidationError):
            first.delete()

    def test_old_form_rejected_after_terms_change(self):
        version = rules_version(self.tournament)
        self.tournament.rules = 'Новая версия'
        self.tournament.save()
        with self.assertRaises(ValidationError):
            accept_rules(self.tournament, self.user, version, True)
        self.assertFalse(RulesAcceptance.objects.exists())

    def test_fee_terms_change_requires_new_acceptance_but_registration_closure_does_not(self):
        accept_rules(self.tournament, self.user, rules_version(self.tournament), True)
        self.tournament.registration_open = True
        self.tournament.save()
        self.assertTrue(has_accepted(self.tournament, self.user))
        self.tournament.prize_pool = 12345
        self.tournament.save()
        self.assertFalse(has_accepted(self.tournament, self.user))

    def test_explicit_consent_no_get_side_effect_and_no_external_redirect(self):
        response = self.client.get(reverse('rules'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RulesAcceptance.objects.exists())
        self.client.post(reverse('rules'), {'version': rules_version(self.tournament)})
        self.assertFalse(RulesAcceptance.objects.exists())
        response = self.client.post(reverse('rules'), {'version': rules_version(self.tournament), 'consent': 'on', 'next': '//evil.example'})
        self.assertRedirects(response, '/captain/', fetch_redirect_response=False)
        self.assertTrue(has_accepted(self.tournament, self.user))

    def test_join_redirect_preserves_invite_before_consuming_post(self):
        invite = reverse('team_join', args=[self.team.invite_token])
        response = self.client.post(invite, {'role': 'main', 'accept_rules': 'on'})
        self.assertTrue(response.url.startswith('/rules/?next='))
        self.assertFalse(self.team.participants.exists())
        self.assertFalse(RulesAcceptance.objects.exists())

    def test_submit_does_not_implicitly_accept(self):
        response = self.client.post(reverse('team_submit', args=[self.team.pk]))
        self.assertTrue(response.url.startswith('/rules/'))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, 'draft')
