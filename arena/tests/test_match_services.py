from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from arena.bracket import generate_bracket, record_result
from arena.match_services import (correct_forfeit, correct_result, mark_ready, record_forfeit, review_report,
                                  start_match, submit_report, update_match_details)
from arena.models import Match, MatchAudit, MatchReport, Notification
from arena.tests import test_brackets as bracket_fixture


class MatchDayTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create(username="judge", is_staff=True)
        self.outsider = get_user_model().objects.create(username="outsider")
        self.tournament, self.teams = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 4)
        generate_bracket(self.tournament, self.staff)
        self.match = self.tournament.matches.get(key="W1-1")

    def test_only_assigned_captain_can_ready_and_start_needs_two_captains(self):
        for actor in (self.outsider, self.staff):
            with self.assertRaises(PermissionDenied):
                mark_ready(self.match, actor)
        mark_ready(self.match, self.match.team_a.captain)
        with self.assertRaises(ValidationError):
            start_match(self.match, self.staff)
        first = self.match.audits.count()
        mark_ready(self.match, self.match.team_a.captain)
        self.assertEqual(self.match.audits.count(), first)
        mark_ready(self.match, self.match.team_b.captain)
        with self.assertRaises(PermissionDenied):
            start_match(self.match, self.match.team_a.captain)
        started = start_match(self.match, self.staff)
        self.assertIsNotNone(started.started_at)
        with self.assertRaises(ValidationError):
            mark_ready(self.match, self.match.team_a.captain)

    def test_deactivated_actor_and_revoked_staff_are_checked_from_database(self):
        captain = self.match.team_a.captain
        get_user_model().objects.filter(pk=captain.pk).update(is_active=False)
        self.assertTrue(captain.is_active)
        with self.assertRaises(PermissionDenied):
            mark_ready(self.match, captain)
        with self.assertRaises(PermissionDenied):
            submit_report(self.match, captain, kind="dispute", message="Старый сеанс")
        get_user_model().objects.filter(pk=self.staff.pk).update(is_staff=False)
        self.assertTrue(self.staff.is_staff)
        with self.assertRaises(PermissionDenied):
            record_result(self.match, 2, 0, self.staff)
        with self.assertRaises(PermissionDenied):
            record_forfeit(self.match, self.match.team_a_id, self.staff, "Старый сеанс")
        self.assertFalse(self.match.audits.exists())

    def test_stale_match_object_cannot_submit_actions_for_replaced_opponent(self):
        record_result(self.match, 2, 0, self.staff)
        other = self.tournament.matches.get(key="W1-2")
        record_result(other, 2, 0, self.staff)
        stale_next = self.tournament.matches.get(key="W2-1")
        unaffected_captain = stale_next.team_b.captain
        correct_result(self.match, 0, 2, self.staff, "Перепутаны стороны")
        with self.assertRaises(ValidationError):
            mark_ready(stale_next, unaffected_captain)
        with self.assertRaises(ValidationError):
            submit_report(stale_next, unaffected_captain, kind="result", message="Счёт по старой странице", reported_score_a=2, reported_score_b=0)
        with self.assertRaises(ValidationError):
            record_result(stale_next, 2, 0, self.staff)
        current = Match.objects.get(pk=stale_next.pk)
        self.assertIsNone(current.ready_b_at)
        self.assertIsNone(current.completed_at)
        self.assertFalse(current.reports.exists())

    def test_deadline_does_not_automatically_forfeit_and_reschedule_resets_readiness(self):
        future = self.tournament.starts_at + timedelta(hours=1)
        update_match_details(self.match, self.staff, scheduled_at=future, ready_deadline=future,
                             lobby_details="private password")
        mark_ready(self.match, self.match.team_a.captain)
        updated = update_match_details(self.match, self.staff, scheduled_at=future,
            ready_deadline=timezone.now()-timedelta(seconds=1), lobby_details="private password")
        self.assertIsNone(updated.ready_a_at)
        with self.assertRaises(ValidationError):
            mark_ready(self.match, self.match.team_b.captain)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.completed_at)
        self.assertFalse(Notification.objects.filter(body__contains="private password").exists())

    def test_invalid_config_and_closed_matches_reject_changes(self):
        with self.assertRaises(PermissionDenied):
            update_match_details(self.match, self.outsider, scheduled_at=None, ready_deadline=None, lobby_details="")
        with self.assertRaises(ValidationError):
            update_match_details(self.match, self.staff, scheduled_at=timezone.now().replace(tzinfo=None), ready_deadline=None, lobby_details="")
        record_result(self.match, 2, 0, self.staff)
        with self.assertRaises(ValidationError):
            update_match_details(self.match, self.staff, scheduled_at=None, ready_deadline=None, lobby_details="")

    def test_forfeit_is_explicit_audited_and_advances_both_sides(self):
        with self.assertRaises(PermissionDenied):
            record_forfeit(self.match, self.match.team_a_id, self.outsider, "Неявка")
        with self.assertRaises(ValidationError):
            record_forfeit(self.match, self.match.team_a_id, self.staff, " ")
        with self.assertRaises(ValidationError):
            record_forfeit(self.match, 999999, self.staff, "Неявка")
        result = record_forfeit(self.match, self.match.team_a_id, self.staff, "Неявка по решению судьи")
        self.assertEqual(result.resolution, Match.Resolution.FORFEIT)
        self.assertIsNone(result.score_a)
        self.assertIsNone(result.score_b)
        self.assertEqual(result.loser.pk, self.match.team_b_id)
        self.assertEqual(self.tournament.matches.get(key="W2-1").team_a_id, result.winner_id)
        self.assertEqual(self.tournament.matches.get(key="L1-1").team_a_id, result.loser.pk)
        audit = result.audits.get(action="forfeit")
        self.assertEqual(audit.actor_id, self.staff.pk)
        self.assertEqual(audit.reason, "Неявка по решению судьи")
        with self.assertRaises(ValidationError):
            record_forfeit(self.match, self.match.team_b_id, self.staff, "Другое решение")

    def test_claim_is_not_result_and_has_one_pending_per_kind_and_team(self):
        captain = self.match.team_a.captain
        with self.assertRaises(PermissionDenied):
            submit_report(self.match, self.outsider, kind="result", message="Победа", reported_score_a=2, reported_score_b=0)
        report = submit_report(self.match, captain, kind="result", message="Победа", evidence_url="https://example.com/result.png", reported_score_a=2, reported_score_b=0)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.winner_id)
        self.assertIsNone(self.match.completed_at)
        with self.assertRaises(ValidationError):
            submit_report(self.match, captain, kind="result", message="Ещё раз", reported_score_a=2, reported_score_b=0)
        submit_report(self.match, captain, kind="dispute", message="Есть вопрос к судье")
        with self.assertRaises(PermissionDenied):
            review_report(report, captain, status="resolved", reason="Да")
        with self.assertRaises(ValidationError):
            review_report(report, self.staff, status="resolved", reason="")
        reviewed = review_report(report, self.staff, status="resolved", reason="Проверено, итог будет внесён отдельно")
        self.assertEqual(reviewed.reviewed_by_id, self.staff.pk)
        self.assertIsNotNone(reviewed.reviewed_at)
        self.match.refresh_from_db()
        self.assertIsNone(self.match.completed_at)
        with self.assertRaises(ValidationError):
            review_report(reviewed, self.staff, status="rejected", reason="Повторное решение")

    def test_report_validates_score_kind_and_evidence_url(self):
        captain = self.match.team_a.captain
        for url in ("javascript:alert(1)", "http://example.com/evidence", "https://user:pass@example.com/file"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                submit_report(self.match, captain, kind="dispute", message="Спор", evidence_url=url)
        for score in ((1, 1), (3, 0), (True, 0), (None, None)):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                submit_report(self.match, captain, kind="result", message="Результат", reported_score_a=score[0], reported_score_b=score[1])
        with self.assertRaises(ValidationError):
            submit_report(self.match, captain, kind="unknown", message="Спор")
        self.assertFalse(MatchReport.objects.exists())

    def test_report_evidence_and_decision_history_are_immutable(self):
        report = submit_report(self.match, self.match.team_a.captain, kind="dispute", message="Первоначальное обращение")
        report.message = "Подменённое обращение"
        with self.assertRaises(ValidationError):
            report.save()
        with self.assertRaises(ValidationError):
            MatchReport.objects.filter(pk=report.pk).update(message="Подмена")
        with self.assertRaises(ValidationError):
            report.delete()
        report.refresh_from_db()
        review_report(report, self.staff, status="resolved", reason="Проверено")
        report.refresh_from_db()
        report.decision_reason = "Подмена решения"
        with self.assertRaises(ValidationError):
            report.save()

    def test_result_and_audit_cannot_be_overwritten_without_correction(self):
        record_result(self.match, 2, 1, self.staff)
        audit = self.match.audits.get(action="result")
        self.assertIsNone(audit.before["winner_id"])
        self.assertEqual(audit.after["score_a"], 2)
        with self.assertRaises(ValidationError):
            record_result(self.match, 0, 2, self.staff)
        with self.assertRaises(ValidationError):
            audit.save()
        with self.assertRaises(ValidationError):
            MatchAudit.objects.filter(pk=audit.pk).delete()
        with self.assertRaises(ValidationError):
            MatchAudit.objects.filter(pk=audit.pk).update(reason="changed")

    def test_correction_reassigns_winner_loser_preserves_other_branch_and_requires_reason(self):
        other = self.tournament.matches.get(key="W1-2")
        record_result(self.match, 2, 0, self.staff)
        record_result(other, 2, 1, self.staff)
        other.refresh_from_db()
        snapshot = (other.score_a, other.score_b, other.winner_id, other.completed_at)
        with self.assertRaises(PermissionDenied):
            correct_result(self.match, 0, 2, self.outsider, "Ошибка")
        with self.assertRaises(ValidationError):
            correct_result(self.match, 0, 2, self.staff, " ")
        corrected = correct_result(self.match, 0, 2, self.staff, "Перепутаны стороны")
        self.assertEqual(corrected.winner_id, self.match.team_b_id)
        upper = self.tournament.matches.get(key="W2-1")
        lower = self.tournament.matches.get(key="L1-1")
        self.assertEqual(upper.team_a_id, self.match.team_b_id)
        self.assertEqual(lower.team_a_id, self.match.team_a_id)
        other.refresh_from_db()
        self.assertEqual((other.score_a, other.score_b, other.winner_id, other.completed_at), snapshot)
        self.assertEqual(corrected.audits.get(action="correction").before["score_a"], 2)

    def test_correction_rejected_if_downstream_ready_started_played_or_reported(self):
        record_result(self.match, 2, 0, self.staff)
        record_result(self.tournament.matches.get(key="W1-2"), 2, 0, self.staff)
        downstream = self.tournament.matches.get(key="W2-1")
        for field, value in (("ready_a_at", timezone.now()), ("started_at", timezone.now()), ("resolution", Match.Resolution.PLAYED)):
            Match.objects.filter(pk=downstream.pk).update(**{field: value})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                correct_result(self.match, 0, 2, self.staff, "Ошибка")
            Match.objects.filter(pk=downstream.pk).update(**{field: "" if field == "resolution" else None})
        submit_report(downstream, downstream.team_a.captain, kind="dispute", message="Уже готовимся")
        with self.assertRaises(ValidationError):
            correct_result(self.match, 0, 2, self.staff, "Ошибка")
        self.match.refresh_from_db()
        self.assertEqual(self.match.score_a, 2)
        self.assertFalse(self.match.audits.filter(action="correction").exists())

    def test_correction_recomputes_derived_byes(self):
        tournament, _ = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 3)
        generate_bracket(tournament, self.staff)
        game = tournament.matches.get(key="W1-2")
        record_result(game, 2, 0, self.staff)
        bye = tournament.matches.get(key="L1-1")
        self.assertEqual(bye.resolution, Match.Resolution.BYE)
        self.assertEqual(bye.winner_id, game.team_b_id)
        correct_result(game, 0, 2, self.staff, "Исправление сторон")
        bye.refresh_from_db()
        self.assertEqual(bye.resolution, Match.Resolution.BYE)
        self.assertEqual(bye.winner_id, game.team_a_id)
        self.assertEqual(tournament.matches.get(key="L2-1").team_a_id, game.team_a_id)

    def test_final_correction_activates_and_deactivates_unstarted_reset(self):
        tournament, _ = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 2)
        generate_bracket(tournament, self.staff)
        record_result(tournament.matches.get(key="W1-1"), 2, 0, self.staff)
        final = tournament.matches.get(key="F1")
        record_result(final, 3, 0, self.staff)
        self.assertFalse(tournament.matches.get(key="F2").active)
        correct_result(final, 0, 3, self.staff, "Исправление финала")
        reset = tournament.matches.get(key="F2")
        self.assertTrue(reset.active)
        self.assertEqual(reset.team_a_id, final.team_b_id)
        self.assertEqual(reset.team_b_id, final.team_a_id)
        correct_result(final, 3, 1, self.staff, "Уточнённый итог")
        reset.refresh_from_db()
        self.assertFalse(reset.active)
        self.assertIsNone(reset.completed_at)

    def test_lower_finalist_forfeit_win_activates_reset(self):
        tournament, _ = bracket_fixture.IncompleteBracketTests.tournament_with_teams(self, 2)
        generate_bracket(tournament, self.staff)
        record_result(tournament.matches.get(key="W1-1"), 2, 0, self.staff)
        final = tournament.matches.get(key="F1")
        record_forfeit(final, final.team_b_id, self.staff, "Техническое поражение верхнего финалиста")
        self.assertTrue(tournament.matches.get(key="F2").active)

    def test_correction_of_forfeit_changes_result_and_blocks_started_reset(self):
        record_forfeit(self.match, self.match.team_a_id, self.staff, "Неявка")
        result = correct_result(self.match, 1, 2, self.staff, "Ошибочный технический итог отменён после сверки")
        self.assertEqual(result.resolution, Match.Resolution.PLAYED)
        self.assertEqual(result.winner_id, self.match.team_b_id)

    def test_correct_forfeit_keeps_scores_absent_and_validates_winner(self):
        record_forfeit(self.match, self.match.team_a_id, self.staff, "Неявка")
        for winner in (None, True, 999999):
            with self.subTest(winner=winner), self.assertRaises(ValidationError):
                correct_forfeit(self.match, winner, self.staff, "Перепутана команда")
        corrected = correct_forfeit(self.match, self.match.team_b_id, self.staff, "Перепутана команда при назначении ТП")
        self.assertEqual(corrected.resolution, Match.Resolution.FORFEIT)
        self.assertEqual(corrected.winner_id, self.match.team_b_id)
        self.assertIsNone(corrected.score_a)
        self.assertIsNone(corrected.score_b)
        self.assertEqual(self.tournament.matches.get(key="W2-1").team_a_id, self.match.team_b_id)
