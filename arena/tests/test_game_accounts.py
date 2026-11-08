from datetime import timedelta
import re
import html
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena import services
from arena.game_accounts import GameAccountForm, claim_participant, save_profile
from arena.game_lookup import LookupResult, LookupUnavailable
from arena.models import GameAccount, Participant, Payment, TelegramIdentity, Tournament
from arena.rules import accept_rules, rules_version


@override_settings(MLBB_LOOKUP_PROVIDER='gopay')
class GameAccountTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user('player')
        self.other = get_user_model().objects.create_user('other')
        TelegramIdentity.objects.create(user=self.user, telegram_id=12345)
        TelegramIdentity.objects.create(user=self.other, telegram_id=98765)
        start = timezone.now() + timedelta(days=7)
        self.tournament = Tournament.objects.create(title='DEMO TEST ONLY', event_date=start.date(), starts_at=start, end_registration=start-timedelta(days=1), timezone_name='UTC', max_teams=2, roster_size=1, reserve_limit=0, reserves_pay=False, best_of=3, final_best_of=5, grand_final_reset=False, terms_confirmed=True, registration_open=True, rules='TEST')
        self.team = services.register_team(self.tournament, self.user, 'DEMO TEST', 'TEST', [{'nickname': 'RosterNick', 'game_id': '123', 'server_id': '456', 'is_reserve': False}])
        self.player = self.team.participants.get()
        accept_rules(self.tournament, self.user, rules_version(self.tournament), True)
        accept_rules(self.tournament, self.other, rules_version(self.tournament), True)
        self.data = {'game_id': '123', 'server_id': '456', 'nickname': 'ManualNick', 'accept_identity': True}
        lookup_patch = patch('arena.game_lookup.lookup', side_effect=lambda game_id, server_id: LookupResult('ActualNick', 'gopay', game_id, server_id))
        self.lookup = lookup_patch.start()
        self.addCleanup(lookup_patch.stop)

    def test_profile_requires_lookup_telegram_and_consent(self):
        no_telegram = get_user_model().objects.create_user('unlinked')
        with self.assertRaises(PermissionDenied):
            save_profile(no_telegram, self.data)
        with self.assertRaises(ValidationError):
            save_profile(self.user, {**self.data, 'accept_identity': False})
        profile = save_profile(self.user, self.data)
        self.assertEqual(profile.nickname, 'ActualNick')
        self.assertEqual(profile.lookup_provider, 'gopay')
        self.assertIsNotNone(profile.lookup_checked_at)

    @override_settings(MLBB_LOOKUP_PROVIDER='disabled')
    def test_disabled_provider_never_accepts_manual_nickname_in_service_or_post(self):
        with self.assertRaises(ValidationError):
            save_profile(self.user, self.data)
        self.client.force_login(self.user)
        for action in ('lookup', 'save'):
            response = self.client.post(reverse('game_account'), {**self.data, 'action': action}, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json()['ok'])
            self.assertFalse(response.json()['saved'])
        self.assertFalse(GameAccount.objects.exists())
        self.lookup.assert_not_called()

    def test_duplicate_game_identity_and_roster_mismatch_rejected(self):
        save_profile(self.user, self.data)
        with self.assertRaises(ValidationError):
            save_profile(self.other, self.data)
        save_profile(self.other, {**self.data, 'game_id': '999'})
        with self.assertRaises(ValidationError):
            claim_participant(self.other, self.player.token)
        self.player.refresh_from_db()
        self.assertIsNone(self.player.account_id)

    def test_claim_binds_only_matching_profile_and_freezes_game_identity(self):
        save_profile(self.user, self.data)
        claimed = claim_participant(self.user, self.player.token)
        self.assertEqual(claimed.account_id, self.user.pk)
        self.assertTrue(claimed.registration_confirmed)
        with self.assertRaises(PermissionDenied):
            claim_participant(self.other, self.player.token)
        with self.assertRaises(ValidationError):
            save_profile(self.user, {**self.data, 'game_id': '789'})

    def test_legacy_token_cannot_make_captain_a_player_of_another_team(self):
        second = services.register_team(self.tournament, self.other, 'Second', 'TEST', [
            {'nickname': 'Second player', 'game_id': '999', 'server_id': '456', 'is_reserve': False},
        ])
        target = second.participants.get()
        save_profile(self.user, {**self.data, 'game_id': '999'})
        with self.assertRaises(ValidationError):
            claim_participant(self.user, target.token)
        self.client.force_login(self.user)
        response = self.client.post(reverse('participant_confirm', args=[target.token]), {'accept_identity': 'on'})
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertIsNone(target.account_id)
        self.assertFalse(target.registration_confirmed)

    def test_legacy_confirmation_and_payment_require_linked_owner(self):
        confirm = reverse('participant_confirm', args=[self.player.token])
        payment = reverse('payment_submit', args=[self.player.token])
        self.assertEqual(self.client.post(confirm, {'accept_identity': 'on'}).status_code, 302)
        self.player.refresh_from_db()
        self.assertFalse(self.player.registration_confirmed)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(payment).status_code, 403)
        save_profile(self.user, self.data)
        self.client.force_login(self.user)
        self.client.post(confirm, {'accept_identity': 'on'})
        self.client.post(payment, {'status': 'confirmed', 'amount': 0})
        self.player.payment.refresh_from_db()
        self.assertEqual(self.player.payment.status, Payment.Status.REVIEW)
        self.assertEqual(self.player.payment.amount, 2200)

    def test_ids_normalized_and_zero_rejected(self):
        form = GameAccountForm({**self.data, 'game_id': '00123', 'server_id': '00456'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['game_id'], '123')
        self.assertFalse(GameAccountForm({**self.data, 'game_id': '0'}).is_valid())

    @override_settings(MLBB_LOOKUP_PROVIDER='moogold')
    def test_enabled_provider_owns_nickname_and_outage_does_not_silently_trust_input(self):
        with patch('arena.game_lookup.lookup', return_value=LookupResult('ActualNick', 'moogold', '123', '456')):
            profile = save_profile(self.user, self.data)
        self.assertEqual(profile.nickname, 'ActualNick')
        self.assertIsNotNone(profile.lookup_checked_at)
        with patch('arena.game_lookup.lookup', side_effect=LookupUnavailable('test')):
            with self.assertRaises(LookupUnavailable):
                save_profile(self.user, {**self.data, 'nickname': 'ForgedNick'})
        profile.refresh_from_db()
        self.assertEqual(profile.nickname, 'ActualNick')

    def test_return_token_cannot_redirect_to_external_url(self):
        token = self.lookup_json()
        response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'accept_identity': 'on', 'action': 'save', 'participant_token': 'https://example.com/'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response)
        self.assertTrue(response.context['profile_saved'])
        self.assertIsNone(response.context['participant_token'])

    def test_profile_save_stays_on_page_with_link_to_valid_participant(self):
        token = self.lookup_json()
        response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'accept_identity': 'on', 'action': 'save', 'participant_token': str(self.player.token)})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response)
        self.assertTrue(response.context['profile_saved'])
        self.assertEqual(response.context['participant_token'], self.player.token)

    def lookup_json(self, **extra):
        self.client.force_login(self.user)
        with patch('arena.game_lookup.lookup', return_value=LookupResult('FoundNick', 'gopay', '123', '456')):
            response = self.client.post(reverse('game_account'), {'game_id': '123', 'server_id': '456', 'action': 'lookup', **extra}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertFalse(response.json()['saved'])
        token = re.search(r'name="lookup_token"[^>]*value="([^"]+)"', response.json()['html'])
        self.assertIsNotNone(token)
        self.assertFalse(GameAccount.objects.filter(user=self.user).exists())
        return html.unescape(token.group(1))

    @override_settings(MLBB_LOOKUP_PROVIDER='gopay')
    def test_lookup_then_json_save_uses_shown_nickname_without_second_lookup_or_redirect(self):
        token = self.lookup_json()
        with patch('arena.game_lookup.lookup') as external:
            response = self.client.post(reverse('game_account'), {**self.data, 'nickname': 'Forged', 'lookup_token': token, 'action': 'save'}, HTTP_ACCEPT='application/json')
        external.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['saved'])
        self.assertNotIn('Location', response)
        self.assertEqual(GameAccount.objects.get(user=self.user).nickname, 'FoundNick')

    @override_settings(MLBB_LOOKUP_PROVIDER='gopay')
    def test_save_rejects_missing_tampered_other_user_changed_ids_and_expired_receipts(self):
        import time
        token = self.lookup_json()
        for changes in ({'lookup_token': ''}, {'lookup_token': token + 'x'}, {'game_id': '999'}):
            response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'action': 'save', **changes}, HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code, 400)
        self.client.force_login(self.other)
        response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'action': 'save'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.client.force_login(self.user)
        with patch('django.core.signing.time.time', return_value=time.time()+601):
            response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'action': 'save'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(GameAccount.objects.exists())

    def test_receipt_from_enabled_provider_cannot_save_after_provider_disabled(self):
        token = self.lookup_json()
        with override_settings(MLBB_LOOKUP_PROVIDER='disabled'):
            response = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'action': 'save'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['saved'])
        self.assertFalse(GameAccount.objects.exists())

    def test_team_invitation_survives_get_lookup_save_without_auto_join_or_redirect(self):
        invite = str(self.team.invite_token)
        self.client.force_login(self.user)
        session = self.client.session
        session['participant_return_token'] = str(self.player.token)
        session.save()
        initial = self.client.get(reverse('game_account'), {'team_token': invite})
        self.assertEqual(initial.context['team_invite_token'], invite)
        self.assertIsNone(initial.context['participant_token'])
        self.assertEqual(self.client.session['team_return_token'], invite)
        self.assertContains(initial, reverse('team_join', args=[invite]))
        token = self.lookup_json(team_token=invite)
        with patch('arena.game_lookup.lookup') as external:
            saved = self.client.post(reverse('game_account'), {**self.data, 'lookup_token': token, 'action': 'save'})
        external.assert_not_called()
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn('Location', saved)
        self.assertTrue(saved.context['profile_saved'])
        self.assertEqual(saved.context['team_invite_token'], invite)
        self.assertContains(saved, reverse('team_join', args=[invite]))
        self.assertFalse(Participant.objects.filter(account=self.user).exists())
        self.assertEqual(self.client.session['team_return_token'], invite)

    def test_team_return_rejects_external_and_unknown_tokens(self):
        self.client.force_login(self.user)
        for value in ('https://example.com/', '//example.com/', 'not-a-uuid', str(uuid.uuid4())):
            with self.subTest(value=value):
                response = self.client.get(reverse('game_account'), {'team_token': value})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context['team_invite_token'])
                self.assertNotIn('team_return_token', self.client.session)
                self.assertNotIn('Location', response)

    @override_settings(MLBB_LOOKUP_PROVIDER='gopay')
    def test_lookup_failure_does_not_offer_a_saveable_result(self):
        self.client.force_login(self.user)
        with patch('arena.game_lookup.lookup', side_effect=LookupUnavailable('Сервис временно недоступен')):
            response = self.client.post(reverse('game_account'), {'game_id': '123', 'server_id': '456', 'action': 'lookup'}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])
        self.assertFalse(response.json()['saved'])
        self.assertIn('Сервис временно недоступен', response.json()['html'])
        self.assertFalse(GameAccount.objects.exists())
