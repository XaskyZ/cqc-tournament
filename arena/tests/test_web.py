"""Integration checks use an isolated test database; no live Telegram calls."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from unittest.mock import MagicMock, patch
from django.urls import reverse
from django.utils import timezone
from arena import services
from arena.models import GameAccount, Match, Outbox, Payment, Team, TelegramIdentity, TelegramReviewSession, Tournament
from arena.telegram import CashierBot
from arena.rules import accept_rules, rules_version


@override_settings(DEMO_MODE=False, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WebFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        for target in ('arena.game_lookup._open_request', 'arena.telegram.urlopen'):
            blocker = patch(target, side_effect=AssertionError('Real network is forbidden in web integration tests'))
            blocker.start()
            self.addCleanup(blocker.stop)
        start = timezone.now() + timedelta(days=7)
        self.tournament = Tournament.objects.create(title='ТЕСТОВЫЙ ТУРНИР', event_date=start.date(), starts_at=start, end_registration=start - timedelta(days=1), timezone_name='UTC', max_teams=2, roster_size=1, reserve_limit=1, reserves_pay=True, best_of=3, final_best_of=5, grand_final_reset=True, terms_confirmed=True, registration_open=True, rules='Тестовый регламент; только изолированная тестовая база.')
        user = get_user_model()
        self.owner = user.objects.create_user('captain', password='TestOnly-Password-943', first_name='Капитан')
        self.other = user.objects.create_user('other', password='TestOnly-Password-943')
        TelegramIdentity.objects.create(user=self.owner, telegram_id=10001)
        TelegramIdentity.objects.create(user=self.other, telegram_id=10002)
        accept_rules(self.tournament, self.owner, rules_version(self.tournament), True)
        accept_rules(self.tournament, self.other, rules_version(self.tournament), True)
        self.admin = user.objects.create_superuser('staff', password='TestOnly-Admin-321')
        self.team = services.register_team(self.tournament, self.owner, 'ТЕСТ Alpha', 'PRIVATE-CONTACT', [{'nickname': '<script>alert(1)</script>', 'game_id': '123456789', 'server_id': '8765', 'is_reserve': False}])
        self.player = self.team.participants.get()
        self.payment = self.player.payment
        GameAccount.objects.create(user=self.owner, game_id=self.player.game_id, server_id=self.player.server_id, nickname=self.player.nickname)

    def test_public_pages_render_without_leaking_private_roster_data(self):
        for url in ['/', '/teams/', '/schedule/', '/bracket/', '/login/', '/signup/']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotContains(response, 'PRIVATE-CONTACT')
            self.assertNotContains(response, str(self.player.token))
            self.assertNotContains(response, self.player.game_id)

    def test_captain_sees_only_own_private_links_and_no_xss(self):
        self.client.force_login(self.owner)
        response = self.client.get('/captain/')
        self.assertContains(response, str(self.player.token))
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.client.force_login(self.other)
        response = self.client.get('/captain/')
        self.assertNotContains(response, str(self.player.token))
        self.assertNotContains(response, 'PRIVATE-CONTACT')

    def test_participant_page_is_private_and_only_its_own_payment(self):
        response = self.client.get(reverse('participant', args=[self.player.token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store, private')
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        self.assertContains(response, self.payment.code)
        self.assertNotContains(response, 'PRIVATE-CONTACT')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def test_csrf_and_safe_methods_protect_participant_mutations(self):
        url = reverse('payment_submit', args=[self.player.token])
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.AWAITING)

    def test_participant_cannot_forge_amount_or_confirm_payment(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('participant_confirm', args=[self.player.token]), {'accept_identity': 'on'})
        self.client.post(reverse('payment_submit', args=[self.player.token]), {'amount': 1, 'status': 'confirmed', 'reviewed_by_telegram': 855341070})
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, 2200)
        self.assertEqual(self.payment.status, Payment.Status.REVIEW)
        self.assertIsNone(self.payment.reviewed_at)
        self.assertEqual(Outbox.objects.count(), 1)

    def test_nonstaff_cannot_review_or_administer(self):
        self.client.force_login(self.owner)
        for url in ['/organizer/', reverse('review_payment', args=[self.payment.pk])]:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse('team_confirm', args=[self.team.pk])).status_code, 403)

    def test_review_requires_swerka_and_current_submission(self):
        services.confirm_registration(self.player)
        services.submit_payment(self.player)
        self.payment.refresh_from_db()
        self.client.force_login(self.admin)
        url = reverse('review_payment', args=[self.payment.pk])
        response = self.client.get(url)
        self.assertContains(response, self.payment.code)
        payload = {'decision': 'approve', 'bank_reference': 'BANK-TEST-1', 'submission': self.payment.submitted_at.isoformat()}
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REVIEW)
        payload['verified'] = 'on'
        payload['submission'] = 'stale'
        self.assertEqual(self.client.post(url, payload).status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REVIEW)
        payload['submission'] = self.payment.submitted_at.isoformat()
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CONFIRMED)
        self.assertEqual(self.payment.reviewed_by_id, self.admin.pk)

    def test_cashier_cannot_use_participant_code_as_bank_reference(self):
        services.confirm_registration(self.player)
        services.submit_payment(self.player)
        self.payment.refresh_from_db()
        self.client.force_login(self.admin)
        response = self.client.post(reverse('review_payment', args=[self.payment.pk]), {'decision': 'approve', 'bank_reference': self.payment.code, 'verified': 'on', 'submission': self.payment.submitted_at.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.REVIEW)

    def test_team_admission_requires_roster_and_paid_players(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('team_confirm', args=[self.team.pk]))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.PENDING)
        from arena.game_accounts import claim_participant
        claim_participant(self.owner, self.player.token)
        services.submit_payment(self.player)
        services.review_payment(self.payment.pk, True, bank_reference='BANK-TEST-2', actor_user=self.admin)
        self.client.post(reverse('roster_check', args=[self.team.pk]))
        self.client.post(reverse('team_confirm', args=[self.team.pk]))
        self.team.refresh_from_db()
        self.assertEqual(self.team.status, Team.Status.CONFIRMED)

    def test_rejected_team_reason_is_saved_and_visible_to_captain(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('team_reject', args=[self.team.pk]), {'reason': 'ТЕСТ: ошибка состава'})
        self.team.refresh_from_db()
        self.assertEqual(self.team.rejection_reason, 'ТЕСТ: ошибка состава')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get('/captain/'), 'ТЕСТ: ошибка состава')

    def test_team_draft_can_be_created_while_tournament_submission_is_closed(self):
        self.tournament.registration_open = False
        self.tournament.save()
        self.client.force_login(self.other)
        response = self.client.post('/register/', {'name': 'ТЕСТ draft', 'telegram_contact': '@other_captain', 'discord_contact': 'other.captain', 'accept_rules': 'on'})
        self.assertEqual(response.status_code, 302)
        draft = Team.objects.get(captain=self.other)
        self.assertEqual(draft.status, Team.Status.DRAFT)
        GameAccount.objects.create(user=self.other, game_id='333', server_id='1', nickname='Other')
        self.client.post(reverse('team_join', args=[draft.invite_token]), {'role': 'main', 'accept_rules': 'on'})
        self.client.post(reverse('team_submit', args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, Team.Status.DRAFT)
        self.assertIsNone(draft.submitted_at)

    def test_player_reserve_flag_and_fee_are_server_controlled(self):
        self.client.force_login(self.other)
        response = self.client.post('/register/', {'name': 'ТЕСТ Beta', 'telegram_contact': '@other_captain', 'discord_contact': 'other.captain', 'accept_rules': 'on', 'amount': '0'})
        self.assertEqual(response.status_code, 302)
        beta = Team.objects.get(captain=self.other)
        GameAccount.objects.create(user=self.other, game_id='999', server_id='1', nickname='Main')
        self.client.post(reverse('team_join', args=[beta.invite_token]), {'role': 'main', 'accept_rules': 'on', 'is_reserve': 'on', 'amount': '0'})
        reserve = get_user_model().objects.create_user('reserve')
        TelegramIdentity.objects.create(user=reserve, telegram_id=10003)
        accept_rules(self.tournament, reserve, rules_version(self.tournament), True)
        GameAccount.objects.create(user=reserve, game_id='998', server_id='1', nickname='Reserve')
        self.client.force_login(reserve)
        self.client.post(reverse('team_join', args=[beta.invite_token]), {'role': 'reserve', 'accept_rules': 'on', 'amount': '0'})
        self.assertFalse(beta.participants.get(nickname='Main').is_reserve)
        self.assertTrue(beta.participants.get(nickname='Reserve').is_reserve)
        self.assertEqual(sum(p.payment.amount for p in beta.participants.all()), 4400)

    def test_admin_cannot_directly_edit_payment_status(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('admin:arena_payment_change', args=[self.payment.pk]), {'status': 'confirmed', 'amount': 1, '_save': 'Save'})
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'awaiting')
        self.assertEqual(self.payment.amount, 2200)

    def test_parent_match_cannot_be_scheduled_after_child(self):
        parent = Match.objects.create(tournament=self.tournament, key='TEST-P', bracket='upper', round=1, index=1, best_of=3)
        child_time = self.tournament.starts_at + timedelta(hours=2)
        Match.objects.create(tournament=self.tournament, key='TEST-C', bracket='upper', round=2, index=1, best_of=3, source_a=parent, source_a_outcome='winner', scheduled_at=child_time)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('match_edit', args=[parent.pk]), {'scheduled_at': (child_time + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(response.status_code, 200)
        parent.refresh_from_db()
        self.assertIsNone(parent.scheduled_at)

    @override_settings(TELEGRAM_CASHIER_ID=855341070)
    def test_website_submission_bot_decision_and_live_status_share_database(self):
        state_url = reverse('participant_state', args=[self.player.token])
        original = self.client.get(state_url).json()['version']
        self.client.force_login(self.owner)
        self.client.post(reverse('participant_confirm', args=[self.player.token]), {'accept_identity': 'on'})
        self.client.post(reverse('payment_submit', args=[self.player.token]))
        fake = MagicMock()
        fake.call.return_value = {'message_id': 123}
        bot = CashierBot(fake)
        bot.flush_outbox()
        session = TelegramReviewSession.objects.get(payment=self.payment, action='approve')
        callback = {'callback_query': {'id': 'TEST-QUERY', 'from': {'id': 855341070}, 'message': {'chat': {'id': 855341070, 'type': 'private'}}, 'data': 'pr:a:' + session.pk.hex}}
        bot.handle_update(callback)
        bot.handle_update({'message': {'from': {'id': 855341070}, 'chat': {'id': 855341070, 'type': 'private'}, 'reply_to_message': {'message_id': 123}, 'text': 'TEST-BANK-INTEGRATION'}})
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'review')
        callback['callback_query']['data'] = 'pr:y:' + session.pk.hex
        bot.handle_update(callback)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'confirmed')
        self.assertEqual(self.payment.reviewed_by_telegram, 855341070)
        self.assertNotEqual(self.client.get(state_url).json()['version'], original)
        response = self.client.get(reverse('participant', args=[self.player.token]))
        self.assertContains(response, 'Оплата подтверждена')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get('/captain/'), 'Оплата подтверждена')

    def test_live_captain_endpoint_requires_login_and_only_returns_version(self):
        self.assertEqual(self.client.get('/captain/state/').status_code, 302)
        self.client.force_login(self.owner)
        response = self.client.get('/captain/state/')
        self.assertEqual(set(response.json()), {'version'})
        self.assertEqual(response['Cache-Control'], 'no-store, private')
