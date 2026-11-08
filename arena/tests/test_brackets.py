from collections import Counter
from datetime import timedelta
import random

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from arena.bracket import generate_bracket, record_result
from arena.models import Match, Participant, Payment, Team, TelegramIdentity, Tournament


class IncompleteBracketTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create(username="organizer", is_staff=True)

    def tournament_with_teams(self, count, reset=True, capacity=16):
        starts = timezone.now() + timedelta(days=2)
        tournament = Tournament.objects.create(
            registration_open=True, terms_confirmed=True, rules="Test rules",
            starts_at=starts, end_registration=starts - timedelta(hours=1),
            event_date=starts.date(), timezone_name="UTC", max_teams=capacity,
            roster_size=1, reserve_limit=0, reserves_pay=False,
            best_of=3, final_best_of=5, grand_final_reset=reset,
        )
        teams = []
        for number in range(count):
            user = get_user_model().objects.create(username=f"p-{tournament.pk}-{number}")
            TelegramIdentity.objects.create(user=user, telegram_id=10000 + user.pk)
            team = Team.objects.create(tournament=tournament, captain=user, name=f"Team {number}",
                                       contact="test", status=Team.Status.CONFIRMED,
                                       roster_checked_at=timezone.now(), roster_checked_by=self.staff)
            player = Participant.objects.create(team=team, account=user, nickname=f"Player {number}",
                                                game_id=str(user.pk), server_id="1", registration_confirmed=True)
            Payment.objects.create(participant=player, status=Payment.Status.CONFIRMED,
                                   bank_reference=f"BANK-{tournament.pk}-{number}")
            teams.append(team)
        return tournament, teams

    def simulate(self, count, activate_reset, random_seed):
        tournament, teams = self.tournament_with_teams(count)
        generated = generate_bracket(tournament, self.staff)
        size = 1 << (count - 1).bit_length()
        self.assertEqual(len(generated), 2 * size - 1)
        first_round = tournament.matches.filter(bracket=Match.Bracket.UPPER, round=1)
        self.assertEqual(first_round.filter(resolution=Match.Resolution.BYE).count(), size - count)
        self.assertFalse(first_round.filter(team_a__isnull=True, team_b__isnull=True).exists())
        losses = Counter()
        played = 0
        rng = random.Random(random_seed)
        while True:
            available = list(tournament.matches.filter(active=True, completed_at__isnull=True,
                                                       team_a__isnull=False, team_b__isnull=False))
            if not available:
                break
            match = rng.choice(available)
            self.assertNotEqual(match.team_a_id, match.team_b_id)
            self.assertLess(losses[match.team_a_id], 2)
            self.assertLess(losses[match.team_b_id], 2)
            needed = match.best_of // 2 + 1
            winner_b = activate_reset if match.bracket == Match.Bracket.FINAL else rng.choice((True, False))
            result = record_result(match, 0 if winner_b else needed, needed if winner_b else 0, self.staff)
            self.assertEqual(result.resolution, Match.Resolution.PLAYED)
            self.assertIsNotNone(result.loser)
            losses[result.loser.pk] += 1
            played += 1
        self.assertFalse(tournament.matches.filter(active=True, completed_at__isnull=True).exists())
        final = tournament.matches.get(key="F2" if activate_reset else "F1")
        reset = tournament.matches.get(key="F2")
        self.assertEqual(reset.active, activate_reset)
        self.assertEqual(reset.completed_at is not None, activate_reset)
        self.assertLessEqual(losses[final.winner_id], 1)
        self.assertEqual(played, 2 * count - (1 if activate_reset else 2))
        for team in teams:
            if team.pk != final.winner_id:
                self.assertEqual(losses[team.pk], 2, (count, team.name, dict(losses)))
        for node in tournament.matches.exclude(resolution=Match.Resolution.PLAYED).exclude(resolution=""):
            self.assertIsNone(node.score_a)
            self.assertIsNone(node.score_b)
            self.assertIsNone(node.loser)
            self.assertIsNotNone(node.completed_at)
            present = sum(team_id is not None for team_id in (node.team_a_id, node.team_b_id))
            self.assertEqual(present, 1 if node.resolution == Match.Resolution.BYE else 0)
            self.assertEqual(bool(node.winner_id), node.resolution == Match.Resolution.BYE)

    def test_incomplete_fields_conserve_real_losses_with_and_without_triggered_reset(self):
        for count in (3, 5, 6, 7, 9, 15):
            for reset in (False, True):
                with self.subTest(count=count, activate_reset=reset):
                    self.simulate(count, reset, random_seed=count + int(reset))

    def test_smaller_full_field_uses_actual_size_not_configured_capacity(self):
        for count in (2, 4, 8, 16):
            with self.subTest(count=count):
                self.simulate(count, True, random_seed=99)

    def test_unresolved_source_is_not_an_empty_slot(self):
        tournament, _ = self.tournament_with_teams(3)
        generate_bracket(tournament, self.staff)
        bye = tournament.matches.get(key="W1-1")
        game = tournament.matches.get(key="W1-2")
        waiting = tournament.matches.get(key="L1-1")
        self.assertEqual(bye.resolution, Match.Resolution.BYE)
        self.assertIsNone(bye.loser)
        self.assertIsNone(waiting.completed_at)
        self.assertEqual(waiting.resolution, "")
        self.assertIsNone(waiting.team_a_id)
        self.assertIsNone(waiting.team_b_id)
        with self.assertRaises(ValidationError):
            record_result(waiting, 2, 0, self.staff)
        with self.assertRaises(ValidationError):
            record_result(bye, 2, 0, self.staff)
        result = record_result(game, 2, 0, self.staff)
        waiting.refresh_from_db()
        self.assertEqual(waiting.resolution, Match.Resolution.BYE)
        self.assertEqual(waiting.winner_id, result.loser.pk)
        self.assertIsNone(waiting.loser)
        self.assertIsNone(waiting.score_a)
        self.assertIsNone(waiting.score_b)
        next_lower = tournament.matches.get(key="L2-1")
        self.assertIsNone(next_lower.completed_at)
        self.assertEqual(next_lower.team_a_id, result.loser.pk)
        self.assertIsNone(next_lower.team_b_id)

    def test_two_empty_sources_resolve_without_fabricating_loser(self):
        tournament, _ = self.tournament_with_teams(5)
        generate_bracket(tournament, self.staff)
        empty = tournament.matches.get(key="L1-2")
        self.assertEqual(empty.resolution, Match.Resolution.EMPTY)
        self.assertIsNotNone(empty.completed_at)
        self.assertIsNone(empty.winner_id)
        self.assertIsNone(empty.loser)
        pending = tournament.matches.get(key="L2-1")
        self.assertIsNone(pending.completed_at)
        self.assertEqual(pending.resolution, "")
        with self.assertRaises(ValidationError):
            record_result(empty, 2, 0, self.staff)

    def test_disabled_reset_has_one_final_even_when_lower_wins(self):
        tournament, _ = self.tournament_with_teams(5, reset=False)
        generate_bracket(tournament, self.staff)
        self.assertFalse(tournament.matches.filter(key="F2").exists())
        while True:
            match = tournament.matches.filter(active=True, completed_at__isnull=True,
                                              team_a__isnull=False, team_b__isnull=False).first()
            if match is None:
                break
            needed = match.best_of // 2 + 1
            record_result(match, 0, needed, self.staff)
        final = tournament.matches.get(key="F1")
        self.assertEqual(final.winner_id, final.team_b_id)
        self.assertFalse(tournament.matches.filter(active=True, completed_at__isnull=True).exists())
        self.assertEqual(tournament.matches.filter(resolution=Match.Resolution.PLAYED).count(), 8)

    def test_permissions_capacity_and_repeat_generation_still_enforced(self):
        tournament, teams = self.tournament_with_teams(3)
        with self.assertRaises(PermissionDenied):
            generate_bracket(tournament, teams[0].captain)
        Tournament.objects.filter(pk=tournament.pk).update(max_teams=2)
        with self.assertRaises(ValidationError):
            generate_bracket(tournament, self.staff)
        self.assertFalse(tournament.matches.exists())
        Tournament.objects.filter(pk=tournament.pk).update(max_teams=16)
        generate_bracket(tournament, self.staff)
        with self.assertRaises(ValidationError):
            generate_bracket(tournament, self.staff)
