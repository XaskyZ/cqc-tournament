"""HTTP contracts for team invitations; isolated DB, mock external providers."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena import team_services
from arena.game_lookup import LookupResult, LookupUnavailable
from arena.models import GameAccount, Participant, Payment, Team, TelegramIdentity, TelegramLoginChallenge, Tournament
from arena.telegram import CashierBot
from arena.telegram_auth import SESSION_KEY
from arena.rules import accept_rules, rules_version


@override_settings(DEMO_MODE=False, MLBB_LOOKUP_PROVIDER='disabled', TELEGRAM_BOT_TOKEN='test-token',
                   TELEGRAM_BOT_USERNAME='test_tournament_bot',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class TeamViewTests(TestCase):
    def setUp(self):
        cache.clear()
        for target in ('arena.game_lookup._open_request', 'arena.telegram.urlopen'):
            blocker = patch(target, side_effect=AssertionError('Real network is forbidden in team HTTP tests'))
            blocker.start()
            self.addCleanup(blocker.stop)
        start = timezone.now() + timedelta(days=5)
        self.tournament = Tournament.objects.create(
            title='Invite test', event_date=start.date(), starts_at=start,
            end_registration=start - timedelta(days=1), timezone_name='UTC',
            max_teams=2, roster_size=2, reserve_limit=1, reserves_pay=False,
            best_of=3, final_best_of=5, grand_final_reset=True,
            terms_confirmed=True, registration_open=True, rules='Test rules',
        )
        self.captain = self.user('captain', 10101)
        self.member = self.user('member', 10102, '875431290', '7721', 'Member')
        self.other = self.user('other', 10103, '965431280', '8822', 'Other')
        self.outsider = self.user('outsider', 10104, '765431270', '9923', 'Outsider')
        self.team = team_services.create_team(self.tournament, self.captain, 'Alpha', '@captain_private', 'captain.private')
        self.invite = reverse('team_join', args=[self.team.invite_token])

    def user(self, name, telegram_id, game_id=None, server_id=None, nickname=None):
        user = get_user_model().objects.create_user(name)
        TelegramIdentity.objects.create(user=user, telegram_id=telegram_id, username=name + '_tg')
        accept_rules(self.tournament, user, rules_version(self.tournament), True)
        if game_id:
            GameAccount.objects.create(user=user, game_id=game_id, server_id=server_id, nickname=nickname)
        return user

    def join(self, user, role='main', **extra):
        self.client.force_login(user)
        accept_rules(self.tournament, user, rules_version(self.tournament), True)
        return self.client.post(self.invite, {'role': role, 'accept_rules': 'on', **extra})

    def test_create_draft_requires_contacts_but_no_game_profile_or_roster(self):
        creator = self.user('new_captain', 10105)
        self.client.force_login(creator)
        response = self.client.post(reverse('register'), {
            'name': 'New team', 'telegram_contact': '@new_captain', 'discord_contact': 'new.captain',
            'accept_rules': 'on', 'status': 'confirmed', 'captain': self.outsider.pk,
            'game_id': self.member.game_account.game_id, 'amount': '0',
        })
        self.assertRedirects(response, reverse('captain'), fetch_redirect_response=False)
        team = Team.objects.get(captain=creator)
        self.assertEqual(team.status, Team.Status.DRAFT)
        self.assertIsNone(team.submitted_at)
        self.assertIsNotNone(team.invite_token)
        self.assertFalse(team.participants.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(GameAccount.objects.filter(user=creator).exists())
        self.assertIn('Telegram: @new_captain', team.contact)
        self.assertIn('Discord: new.captain', team.contact)

    def test_create_requires_telegram_and_both_contacts(self):
        unlinked = get_user_model().objects.create_user('unlinked')
        self.client.force_login(unlinked)
        response = self.client.post(reverse('register'), {'name': 'Denied', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Team.objects.filter(captain=unlinked).exists())
        self.client.force_login(self.outsider)
        response = self.client.post(reverse('register'), {'name': 'Incomplete', 'telegram_contact': '@outsider_tg', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('discord_contact', response.context['form'].errors)
        self.assertFalse(Team.objects.filter(captain=self.outsider).exists())

    def test_guest_invitation_exposes_public_roster_only(self):
        first = team_services.join_team(self.team, self.member)
        second = team_services.join_team(self.team, self.other)
        response = self.client.get(self.invite)
        self.assertContains(response, 'Alpha')
        self.assertContains(response, 'Member')
        self.assertContains(response, 'Other')
        for private in (str(first.token), str(second.token), first.game_id, second.game_id,
                        first.server_id, second.server_id, 'captain_private', 'captain.private', first.payment.code):
            self.assertNotContains(response, private)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.client.session['team_return_token'], str(self.team.invite_token))
        self.assertNotIn('participant_return_token', self.client.session)
        self.assertEqual(self.client.post(self.invite, {'role': 'main', 'accept_rules': 'on'}).status_code, 302)
        self.assertEqual(Participant.objects.count(), 2)

    def test_join_uses_own_profile_and_never_confirms_payment_or_team(self):
        response = self.join(self.member, amount='0', status='confirmed', account_id=self.outsider.pk,
                             game_id='999999', server_id='5555', nickname='Forged', is_reserve='on')
        self.assertRedirects(response, reverse('captain'), fetch_redirect_response=False)
        player = self.team.participants.get()
        self.assertEqual(player.account_id, self.member.pk)
        self.assertEqual((player.game_id, player.server_id), (self.member.game_account.game_id, self.member.game_account.server_id))
        self.assertEqual(player.nickname, 'Member')
        self.assertTrue(player.registration_confirmed)
        self.assertFalse(player.is_reserve)
        self.assertEqual(player.payment.status, Payment.Status.AWAITING)
        self.assertEqual(player.payment.amount, 2200)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)
        self.assertNotIn('team_return_token', self.client.session)
        self.join(self.member, role='reserve')
        player.refresh_from_db()
        self.assertFalse(player.is_reserve)
        self.assertEqual(self.team.participants.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_join_requires_profile_telegram_and_explicit_consent(self):
        self.client.force_login(self.captain)
        response = self.client.post(self.invite, {'role': 'main', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.team.participants.exists())
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.invite, {'role': 'main'}).status_code, 200)
        self.assertFalse(self.team.participants.exists())
        TelegramIdentity.objects.filter(user=self.member).delete()
        self.assertEqual(self.client.post(self.invite, {'role': 'main', 'accept_rules': 'on'}).status_code, 403)
        self.assertFalse(self.team.participants.exists())

    def test_member_dashboard_finds_team_without_other_players_private_data(self):
        first = team_services.join_team(self.team, self.member)
        second = team_services.join_team(self.team, self.other)
        self.client.force_login(self.member)
        response = self.client.get(reverse('captain'))
        self.assertEqual(response.context['team'].pk, self.team.pk)
        self.assertFalse(response.context['is_captain'])
        self.assertContains(response, reverse('participant', args=[first.token]))
        self.assertContains(response, 'Other')
        for private in (str(second.token), second.game_id, second.server_id, second.payment.code, 'captain_private', 'captain.private'):
            self.assertNotContains(response, private)
        self.assertNotContains(response, reverse('team_submit', args=[self.team.pk]))
        self.assertNotContains(response, reverse('team_add_player', args=[self.team.pk]))
        self.assertContains(response, reverse('team_remove_player', args=[self.team.pk, first.pk]))
        self.assertNotContains(response, reverse('team_remove_player', args=[self.team.pk, second.pk]))
        state = self.client.get(reverse('captain_state'))
        self.assertEqual(set(state.json()), {'version'})
        self.assertIn('no-store', state.headers['Cache-Control'])

    def test_captain_dashboard_has_shared_invite_and_own_roster(self):
        player = team_services.join_team(self.team, self.member)
        self.client.force_login(self.captain)
        response = self.client.get(reverse('captain'))
        self.assertContains(response, self.invite)
        self.assertContains(response, str(player.token))
        self.assertContains(response, player.game_id)
        self.assertContains(response, 'captain.private')
        self.assertContains(response, reverse('team_remove_player', args=[self.team.pk, player.pk]))
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('captain'))
        self.assertIsNone(response.context['team'])
        self.assertNotContains(response, str(player.token))
        self.assertNotContains(response, 'captain.private')

    def test_only_captain_can_submit_and_incomplete_or_closed_registration_blocks(self):
        self.client.force_login(self.captain)
        url = reverse('team_submit', args=[self.team.pk])
        self.client.post(url)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)
        team_services.join_team(self.team, self.member)
        team_services.join_team(self.team, self.other)
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.captain)
        self.tournament.registration_open = False
        self.tournament.save()
        self.client.post(url)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)
        self.tournament.registration_open = True
        self.tournament.save()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.team.refresh_from_db()
        submitted = self.team.submitted_at
        self.assertEqual(self.team.status, Team.Status.PENDING)
        self.assertIsNotNone(submitted)
        self.assertTrue(all(payment.status == Payment.Status.AWAITING for payment in Payment.objects.all()))
        self.client.post(url)
        self.team.refresh_from_db()
        self.assertEqual(self.team.submitted_at, submitted)
        self.assertEqual(self.join(self.outsider, 'reserve').status_code, 200)
        self.assertEqual(self.team.participants.count(), 2)

    def test_manual_add_captain_only_uses_lookup_and_member_claims_same_row(self):
        url = reverse('team_add_player', args=[self.team.pk])
        payload = {'game_id': self.member.game_account.game_id, 'server_id': self.member.game_account.server_id, 'role': 'main', 'nickname': 'Forged'}
        result = LookupResult('Member', 'test-provider', payload['game_id'], payload['server_id'])
        with patch('arena.team_services.game_lookup.lookup', return_value=result) as lookup:
            self.client.force_login(self.outsider)
            self.assertEqual(self.client.post(url, payload).status_code, 403)
            lookup.assert_not_called()
            self.client.force_login(self.captain)
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            lookup.assert_called_once_with(payload['game_id'], payload['server_id'])
        player = self.team.participants.get()
        original_token, original_payment = player.token, player.payment.pk
        self.assertEqual(player.nickname, 'Member')
        self.assertIsNone(player.account_id)
        self.assertFalse(player.registration_confirmed)
        self.join(self.member)
        player.refresh_from_db()
        self.assertEqual(player.account_id, self.member.pk)
        self.assertTrue(player.registration_confirmed)
        self.assertEqual(player.token, original_token)
        self.assertEqual(player.payment.pk, original_payment)
        self.assertEqual(self.team.participants.count(), 1)

    def test_failed_manual_lookup_does_not_reserve_place(self):
        self.client.force_login(self.captain)
        with patch('arena.team_services.game_lookup.lookup', side_effect=LookupUnavailable('Test unavailable')):
            response = self.client.post(reverse('team_add_player', args=[self.team.pk]), {'game_id': '12345', 'server_id': '1234', 'role': 'main'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.team.participants.exists())
        self.assertFalse(Payment.objects.exists())

    def test_remove_is_captain_or_self_only_and_does_not_touch_paid_history(self):
        player = team_services.join_team(self.team, self.member)
        other = team_services.join_team(self.team, self.other)
        url = reverse('team_remove_player', args=[self.team.pk, player.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertTrue(Participant.objects.filter(pk=player.pk).exists())
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(Participant.objects.filter(pk=player.pk).exists())
        self.assertFalse(Payment.objects.filter(participant_id=player.pk).exists())
        other.payment.status = Payment.Status.REVIEW
        other.payment.save(update_fields=['status'])
        self.client.force_login(self.captain)
        self.client.post(reverse('team_remove_player', args=[self.team.pk, other.pk]))
        self.assertTrue(Participant.objects.filter(pk=other.pk).exists())
        self.assertTrue(Payment.objects.filter(participant_id=other.pk).exists())

    def test_member_cannot_create_another_team_and_captain_cannot_join_competing_team(self):
        team_services.join_team(self.team, self.member)
        self.client.force_login(self.member)
        response = self.client.post(reverse('register'), {'name': 'Competing', 'telegram_contact': '@member_tg', 'discord_contact': 'member', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Team.objects.filter(captain=self.member).exists())
        competing = team_services.create_team(self.tournament, self.outsider, 'Competing', '@outsider_tg', 'outsider')
        GameAccount.objects.create(user=self.captain, game_id='123456711', server_id='1231', nickname='Captain')
        self.client.force_login(self.captain)
        response = self.client.post(reverse('team_join', args=[competing.invite_token]), {'role': 'main', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(competing.participants.filter(account=self.captain).exists())

    def test_safe_methods_and_csrf_protect_team_mutations(self):
        player = team_services.join_team(self.team, self.member)
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.captain)
        urls = [reverse('team_add_player', args=[self.team.pk]), reverse('team_submit', args=[self.team.pk]),
                reverse('team_remove_player', args=[self.team.pk, player.pk])]
        for url in urls:
            self.assertEqual(browser.get(url).status_code, 405)
            self.assertEqual(browser.post(url, {'role': 'main', 'accept_rules': 'on'}).status_code, 403)
        self.assertEqual(browser.post(self.invite, {'role': 'main', 'accept_rules': 'on'}).status_code, 403)
        self.assertEqual(browser.post(reverse('register'), {'name': 'CSRF'}).status_code, 403)
        self.assertEqual(self.team.participants.count(), 1)
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.DRAFT)

    def test_telegram_login_returns_to_invitation_without_automatic_join(self):
        self.client.get(self.invite)
        response = self.client.post(reverse('telegram_auth_begin'), {'purpose': 'login'})
        self.assertEqual(response.status_code, 302)
        saved = self.client.session[SESSION_KEY]
        challenge = TelegramLoginChallenge.objects.get(pk=saved['id'])
        api = MagicMock()
        api.call.return_value = {'message_id': 100}
        bot = CashierBot(api)
        identity = self.member.telegram_identity.telegram_id
        message = {'from': {'id': identity, 'first_name': 'Member'}, 'chat': {'id': identity, 'type': 'private'}, 'text': '/start login_' + saved['nonce']}
        bot.handle_update({'message': message})
        bot.handle_update({'message': {**message, 'text': saved['code'], 'reply_to_message': {'message_id': 100}}})
        bot.handle_update({'callback_query': {'id': 'test', 'data': f'ta:y:{challenge.pk.hex}', 'from': message['from'], 'message': {'chat': message['chat']}}})
        response = self.client.post(reverse('telegram_auth_finish'), {'confirmed': 'on', 'telegram_id': identity})
        self.assertRedirects(response, self.invite, fetch_redirect_response=False)
        self.assertEqual(self.client.session['team_return_token'], str(self.team.invite_token))
        self.assertFalse(self.team.participants.exists())
        response = self.client.post(self.invite, {'role': 'main', 'accept_rules': 'on'})
        self.assertRedirects(response, reverse('captain'), fetch_redirect_response=False)
        self.assertEqual(self.team.participants.get().account_id, self.member.pk)
        self.assertNotIn('team_return_token', self.client.session)

    @override_settings(MLBB_LOOKUP_PROVIDER='gopay')
    def test_first_time_telegram_user_keeps_invitation_through_profile_save(self):
        self.client.get(self.invite)
        self.client.post(reverse('telegram_auth_begin'), {'purpose': 'login'})
        saved = self.client.session[SESSION_KEY]
        challenge = TelegramLoginChallenge.objects.get(pk=saved['id'])
        api = MagicMock()
        api.call.return_value = {'message_id': 100}
        bot = CashierBot(api)
        identity = 909090
        message = {'from': {'id': identity, 'first_name': 'New player'}, 'chat': {'id': identity, 'type': 'private'}, 'text': '/start login_' + saved['nonce']}
        bot.handle_update({'message': message})
        bot.handle_update({'message': {**message, 'text': saved['code'], 'reply_to_message': {'message_id': 100}}})
        bot.handle_update({'callback_query': {'id': 'new-player', 'data': f'ta:y:{challenge.pk.hex}', 'from': message['from'], 'message': {'chat': message['chat']}}})
        response = self.client.post(reverse('telegram_auth_finish'), {'confirmed': 'on', 'telegram_id': identity})
        self.assertRedirects(response, reverse('game_account'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['team_return_token'], str(self.team.invite_token))
        self.assertFalse(self.team.participants.exists())
        lookup = LookupResult('New player', 'gopay', '123456790', '1234')
        with patch('arena.game_lookup.lookup', return_value=lookup):
            found = self.client.post(reverse('game_account'), {'action': 'lookup', 'game_id': '123456790', 'server_id': '1234'})
        self.assertFalse(found.context['form'].errors, found.context['form'].errors.as_json())
        receipt = found.context['form']['lookup_token'].value()
        response = self.client.post(reverse('game_account'), {
            'action': 'save', 'game_id': '123456790', 'server_id': '1234',
            'nickname': 'Forged input ignored', 'accept_identity': 'on', 'lookup_token': receipt,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.invite)
        self.assertFalse(response.context['form'].errors, response.context['form'].errors.as_json())
        self.assertTrue(GameAccount.objects.filter(user__telegram_identity__telegram_id=identity).exists())
        self.assertFalse(self.team.participants.exists())
        self.client.post(reverse('rules'), {'version': rules_version(self.tournament), 'consent': 'on'})
        joined = self.client.post(self.invite, {'role': 'main', 'accept_rules': 'on'})
        self.assertEqual(joined.status_code, 302, joined.context['form'].errors.as_json() if joined.context else '')
        identity_row = TelegramIdentity.objects.get(telegram_id=identity)
        player = self.team.participants.get()
        self.assertEqual(player.account_id, identity_row.user_id)
        self.assertEqual(player.game_id, '123456790')
        self.assertEqual(player.nickname, 'New player')
        self.assertNotIn('team_return_token', self.client.session)
