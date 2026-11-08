from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arena.bracket import generate_bracket
from arena.match_services import record_forfeit, submit_report
from arena.models import Match, MatchReport, Participant
from arena.match_views import match_revision
from arena.tests import test_brackets as bracket_fixture


@override_settings(DEMO_MODE=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MatchWorkspaceTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.staff = users.objects.create_user('judge-view', is_staff=True)
        self.outsider = users.objects.create_user('outsider-view')
        self.member = users.objects.create_user('member-view')
        self.tournament, self.teams = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 4)
        generate_bracket(self.tournament, self.staff)
        self.match = self.tournament.matches.get(key='W1-1')
        self.match.lobby_details = 'PRIVATE-LOBBY-KEY'
        self.match.save(update_fields=['lobby_details'])
        self.match.team_a.contact = 'PRIVATE-CAPTAIN-CONTACT'
        self.match.team_a.save(update_fields=['contact'])
        self.captain = self.match.team_a.captain
        self.other_captain = self.match.team_b.captain
        self.player = Participant.objects.create(team=self.match.team_a, account=self.member,
            nickname='TestMember', game_id='990011', server_id='1001', registration_confirmed=True)
        self.report = submit_report(self.match, self.captain, kind='dispute', message='PRIVATE-REPORT-CONTENT',
            evidence_url='https://example.com/private-evidence')
        self.detail = reverse('match_detail', args=[self.match.pk])
        self.action = reverse('match_action', args=[self.match.pk])
        self.edit = reverse('match_edit', args=[self.match.pk])

    def post_match(self, url, data, **kwargs):
        return self.client.post(url, {'revision': match_revision(Match.objects.get(pk=self.match.pk)), **data}, **kwargs)

    def test_guest_and_unrelated_user_never_receive_private_details(self):
        for actor in (None, self.outsider):
            if actor:
                self.client.force_login(actor)
            response = self.client.get(self.detail)
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertIn('private', response['Cache-Control'])
            for secret in ('PRIVATE-LOBBY-KEY', 'PRIVATE-CAPTAIN-CONTACT', 'PRIVATE-REPORT-CONTENT', 'private-evidence'):
                self.assertNotContains(response, secret)

    def test_members_and_captains_can_read_lobby_but_opponent_cannot_read_report(self):
        for actor in (self.captain, self.member, self.staff):
            self.client.force_login(actor)
            response = self.client.get(self.detail)
            self.assertContains(response, 'PRIVATE-LOBBY-KEY')
            self.assertContains(response, 'PRIVATE-REPORT-CONTENT')
            self.assertContains(response, 'rel="noopener noreferrer nofollow"')
        self.client.force_login(self.other_captain)
        response = self.client.get(self.detail)
        self.assertContains(response, 'PRIVATE-LOBBY-KEY')
        self.assertNotContains(response, 'PRIVATE-REPORT-CONTENT')

    def test_removed_player_loses_private_access(self):
        self.player.active = False
        self.player.save(update_fields=['active'])
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.detail), 'PRIVATE-LOBBY-KEY')

    def test_ready_requires_captain_post_and_csrf(self):
        self.assertEqual(self.client.get(self.action).status_code, 405)
        self.client.force_login(self.member)
        self.assertEqual(self.post_match(self.action, {'action': 'ready'}).status_code, 403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.post_match(self.action, {'action': 'ready'}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.captain)
        self.assertEqual(csrf.post(self.action, {'action': 'ready'}).status_code, 403)
        self.client.force_login(self.captain)
        response = self.post_match(self.action, {'action': 'ready'})
        self.assertEqual(response.status_code, 302)
        self.match.refresh_from_db()
        self.assertIsNotNone(self.match.ready_a_at)
        self.assertIsNone(self.match.ready_b_at)
        self.assertEqual(self.post_match(self.action, {'action': 'start'}).status_code, 403)

    def test_expired_deadline_error_does_not_change_result(self):
        self.match.ready_deadline = timezone.now() - timedelta(minutes=1)
        self.match.save(update_fields=['ready_deadline'])
        self.client.force_login(self.captain)
        response = self.post_match(self.action, {'action': 'ready'}, follow=True)
        self.assertContains(response, 'Срок готовности истёк')
        self.match.refresh_from_db()
        self.assertIsNone(self.match.winner_id)
        self.assertIsNone(self.match.ready_a_at)

    def test_result_claim_is_not_an_official_result_and_duplicate_is_visible_error(self):
        self.client.force_login(self.captain)
        data = {'action': 'report', 'kind': 'result', 'reported_score_a': 2, 'reported_score_b': 0,
            'message': 'Our result', 'evidence_url': 'https://example.com/game'}
        self.assertEqual(self.post_match(self.action, data).status_code, 302)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.winner_id)
        self.assertIsNone(self.match.completed_at)
        response = self.post_match(self.action, data)
        self.assertContains(response, 'уже ожидает рассмотрения')
        self.assertEqual(MatchReport.objects.filter(match=self.match, kind='result').count(), 1)

    def test_report_validates_link_score_and_escapes_text(self):
        self.client.force_login(self.other_captain)
        data = {'action': 'report', 'kind': 'result', 'message': '<script>alert(1)</script>',
            'reported_score_a': 0, 'reported_score_b': 0, 'evidence_url': 'javascript:alert(1)'}
        self.assertEqual(self.post_match(self.action, data).status_code, 200)
        self.assertEqual(MatchReport.objects.filter(team=self.match.team_b).count(), 0)
        data.update(kind='dispute', reported_score_a='', reported_score_b='', evidence_url='https://example.com/evidence')
        response = self.post_match(self.action, data, follow=True)
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_management_and_review_are_staff_only(self):
        self.client.force_login(self.captain)
        review = reverse('match_report_review', args=[self.report.pk])
        for url in (self.edit, reverse('match_report_queue')):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.post_match(self.edit, {'action': 'result', 'score_a': 2, 'score_b': 0}).status_code, 403)
        self.assertEqual(self.client.post(review, {'status': 'resolved', 'reason': 'fake'}).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(review).status_code, 405)
        self.assertEqual(self.client.get(self.edit).status_code, 200)
        self.assertContains(self.client.get(reverse('match_report_queue')), 'PRIVATE-REPORT-CONTENT')

    def test_staff_review_requires_reason_and_keeps_result_separate(self):
        self.client.force_login(self.staff)
        review = reverse('match_report_review', args=[self.report.pk])
        self.assertEqual(self.client.post(review, {'status': 'resolved'}).status_code, 200)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, 'pending')
        self.assertEqual(self.client.post(review, {'status': 'resolved', 'reason': 'Checked video'}).status_code, 302)
        self.report.refresh_from_db()
        self.assertEqual(self.report.reviewed_by_id, self.staff.pk)
        self.assertIsNotNone(self.report.reviewed_at)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.winner_id)

    def test_staff_result_and_correction_are_audited(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.post_match(self.edit, {'action': 'result', 'score_a': 2, 'score_b': 0}).status_code, 302)
        self.assertEqual(self.post_match(self.edit, {'action': 'correct', 'score_a': 0, 'score_b': 2}).status_code, 200)
        self.match.refresh_from_db()
        self.assertEqual(self.match.winner_id, self.match.team_a_id)
        response = self.post_match(self.edit, {'action': 'correct', 'score_a': 0, 'score_b': 2, 'reason': 'Judge checked recording'})
        self.assertEqual(response.status_code, 302)
        self.match.refresh_from_db()
        self.assertEqual(self.match.winner_id, self.match.team_b_id)
        self.assertTrue(self.match.audits.filter(reason='Judge checked recording', actor=self.staff).exists())

    def test_public_forfeit_and_no_data_are_not_fake_scores(self):
        response = self.client.get(self.detail)
        self.assertContains(response, 'Официальный результат ещё не записан')
        record_forfeit(self.match, self.match.team_b_id, self.staff, 'Команда не явилась')
        response = self.client.get(self.detail)
        self.assertContains(response, 'Техническое поражение')
        self.assertContains(response, 'Команда не явилась')
        self.assertNotContains(response, 'PRIVATE-LOBBY-KEY')

    def test_staff_can_correct_forfeit_without_inventing_played_score(self):
        record_forfeit(self.match, self.match.team_b_id, self.staff, 'Initial decision')
        self.client.force_login(self.staff)
        response = self.post_match(self.edit, {'action': 'correct_forfeit', 'winner_team_id': self.match.team_a_id,
            'reason': 'Reviewed attendance evidence'})
        self.assertEqual(response.status_code, 302)
        self.match.refresh_from_db()
        self.assertEqual(self.match.resolution, 'forfeit')
        self.assertEqual(self.match.winner_id, self.match.team_a_id)
        self.assertIsNone(self.match.score_a)
        self.assertIsNone(self.match.score_b)

    def test_schedule_and_cabinet_link_to_workspace(self):
        self.assertContains(self.client.get(reverse('schedule')), self.detail)
        self.assertContains(self.client.get(reverse('bracket')), self.detail)
        response = self.client.get(reverse('schedule') + f'?team={self.match.team_a_id}')
        self.assertContains(response, f'Матчи команды {self.match.team_a.name}')

    def test_staff_unknown_action_is_rejected(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.post_match(self.edit, {'action': 'delete'}).status_code, 403)

    def test_inactive_account_cannot_read_private_data(self):
        self.member.is_active = False
        self.member.save(update_fields=['is_active'])
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.detail), 'PRIVATE-LOBBY-KEY')

    def test_signed_revision_blocks_stale_and_missing_result_submission(self):
        self.client.force_login(self.staff)
        old_revision = match_revision(self.match)
        self.match.team_a, self.match.team_b = self.match.team_b, self.match.team_a
        self.match.save(update_fields=['team_a', 'team_b'])
        for revision in (old_revision, ''):
            response = self.client.post(self.edit, {'action': 'result', 'score_a': 2, 'score_b': 0, 'revision': revision})
            self.assertEqual(response.status_code, 200)
            self.match.refresh_from_db()
            self.assertIsNone(self.match.winner_id)
        response = self.post_match(self.edit, {'action': 'result', 'score_a': 2, 'score_b': 0})
        self.assertEqual(response.status_code, 302)

    def test_other_side_ready_does_not_invalidate_captain_page(self):
        revision = match_revision(self.match)
        self.client.force_login(self.other_captain)
        self.assertEqual(self.post_match(self.action, {'action': 'ready'}).status_code, 302)
        self.client.force_login(self.captain)
        response = self.client.post(self.action, {'action': 'ready', 'revision': revision})
        self.assertEqual(response.status_code, 302)
        self.match.refresh_from_db()
        self.assertIsNotNone(self.match.ready_a_at)
        self.assertIsNotNone(self.match.ready_b_at)
