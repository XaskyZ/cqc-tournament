"""Real row-lock checks. Run against the isolated PostgreSQL QA database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase

from arena.bracket import generate_bracket, record_result
from arena.match_services import correct_result, mark_ready, submit_report
from arena.models import Match, MatchReport
from arena.tests import test_brackets as bracket_fixture


@skipUnless(connection.vendor == "postgresql", "Requires real PostgreSQL row locking")
class MatchConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create(username="concurrent-judge", is_staff=True)
        self.tournament, self.teams = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 4)
        generate_bracket(self.tournament, self.staff)
        self.match = self.tournament.matches.get(key="W1-1")

    def race(self, *actions):
        barrier = Barrier(len(actions))

        def worker(action):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                try:
                    action()
                except (PermissionDenied, ValidationError) as exc:
                    return ("rejected", type(exc).__name__)
                return ("ok", None)
            finally:
                # A worker must not keep the test database connected after it
                # exits. No network or Telegram delivery takes place here.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [executor.submit(worker, action) for action in actions]
            return [future.result(timeout=45) for future in futures]

    def test_two_captains_ready_concurrently_preserve_both_sides(self):
        match_id = self.match.pk
        captain_ids = (self.match.team_a.captain_id, self.match.team_b.captain_id)

        def ready(captain_id):
            return lambda: mark_ready(Match.objects.get(pk=match_id), get_user_model().objects.get(pk=captain_id))

        outcomes = self.race(*(ready(captain_id) for captain_id in captain_ids))
        self.assertEqual(outcomes, [("ok", None), ("ok", None)])
        self.match.refresh_from_db()
        self.assertIsNotNone(self.match.ready_a_at)
        self.assertIsNotNone(self.match.ready_b_at)
        self.assertEqual(self.match.audits.filter(action="ready").count(), 2)
        self.assertIsNone(self.match.completed_at)

    def test_duplicate_concurrent_report_has_one_pending_record(self):
        match_id, captain_id = self.match.pk, self.match.team_a.captain_id

        def report():
            submit_report(Match.objects.get(pk=match_id), get_user_model().objects.get(pk=captain_id),
                          kind="result", message="Сообщаем результат", reported_score_a=2, reported_score_b=0)

        outcomes = self.race(report, report)
        self.assertCountEqual(outcomes, [("ok", None), ("rejected", "ValidationError")])
        reports = MatchReport.objects.filter(match_id=match_id)
        self.assertEqual(reports.count(), 1)
        self.assertEqual(reports.get().status, MatchReport.Status.PENDING)
        self.assertEqual(self.match.audits.filter(action="report").count(), 1)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.completed_at)
        self.assertIsNone(self.match.winner_id)

    def test_correction_and_downstream_readiness_cannot_both_succeed(self):
        record_result(self.match, 2, 0, self.staff)
        record_result(self.tournament.matches.get(key="W1-2"), 2, 0, self.staff)
        downstream = self.tournament.matches.get(key="W2-1")
        match_id, downstream_id = self.match.pk, downstream.pk
        original_winner_id, corrected_winner_id = self.match.team_a_id, self.match.team_b_id
        old_captain_id, judge_id = downstream.team_a.captain_id, self.staff.pk

        def correction():
            correct_result(Match.objects.get(pk=match_id), 0, 2, get_user_model().objects.get(pk=judge_id), "Перепутаны стороны")

        def ready():
            mark_ready(Match.objects.get(pk=downstream_id), get_user_model().objects.get(pk=old_captain_id))

        outcomes = self.race(correction, ready)
        self.assertEqual(sum(status == "ok" for status, _ in outcomes), 1, outcomes)
        self.match.refresh_from_db()
        downstream.refresh_from_db()
        if outcomes[0][0] == "ok":
            self.assertIn(outcomes[1], [("rejected", "PermissionDenied"), ("rejected", "ValidationError")])
            self.assertEqual(self.match.winner_id, corrected_winner_id)
            self.assertEqual(downstream.team_a_id, corrected_winner_id)
            self.assertIsNone(downstream.ready_a_at)
            self.assertEqual(self.match.audits.filter(action="correction").count(), 1)
        else:
            self.assertEqual(outcomes[0], ("rejected", "ValidationError"))
            self.assertEqual(self.match.winner_id, original_winner_id)
            self.assertEqual(downstream.team_a_id, original_winner_id)
            self.assertIsNotNone(downstream.ready_a_at)
            self.assertFalse(self.match.audits.filter(action="correction").exists())
