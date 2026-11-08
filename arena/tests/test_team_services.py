from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from arena.game_lookup import LookupResult, LookupUnavailable
from arena.models import (GameAccount, Match, Outbox, Participant, Payment,
                          PaymentAudit, Team, TelegramIdentity, TelegramReviewSession, Tournament)
from arena.team_services import (add_player, create_team, join_team, remove_player,
                                 submission_errors, submit_team)
from arena.services import submit_payment
from arena.rules import accept_rules, rules_version


class Fixtures:
    def setUp(self):
        start = timezone.now() + timedelta(days=3)
        self.tournament = Tournament.objects.create(
            registration_open=True, terms_confirmed=True, rules="Rules for tests",
            starts_at=start, end_registration=start - timedelta(hours=1),
            event_date=start.date(), timezone_name="UTC", max_teams=2,
            roster_size=1, reserve_limit=1, reserves_pay=False,
            best_of=3, final_best_of=5, grand_final_reset=True,
        )
        self.captain = self.user("captain")
        self.other = self.user("other")

    def user(self, name, game_id=None):
        user = get_user_model().objects.create(username=name)
        TelegramIdentity.objects.create(user=user, telegram_id=10000 + user.pk)
        accept_rules(self.tournament, user, rules_version(self.tournament), True)
        if game_id:
            GameAccount.objects.create(user=user, game_id=str(game_id), server_id="45", nickname=f"Nick {name}")
        return user

    def team(self, captain=None, name="First"):
        return create_team(self.tournament, captain or self.captain, name, "@captain_name", "discord_name")

    def reserve(self, team, game_id="123", role="main"):
        with patch("arena.team_services.game_lookup.lookup", return_value=LookupResult("API Nick", "gopay", game_id, "45")):
            return add_player(team, self.captain, game_id, "45", role)


class TeamServicesTests(Fixtures, TestCase):
    def test_create_empty_draft_while_registration_closed_without_game_profile(self):
        self.tournament.registration_open = False
        self.tournament.terms_confirmed = False
        self.tournament.save()
        team = self.team()
        self.assertEqual(team.status, Team.Status.DRAFT)
        self.assertIsNone(team.submitted_at)
        self.assertTrue(team.invite_token)
        self.assertEqual(team.contact, "Telegram: @captain_name\nDiscord: discord_name")
        self.assertFalse(team.participants.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(Team._meta.get_field("status").default, Team.Status.PENDING)

    def test_create_auth_consent_names_and_contacts(self):
        self.team()
        for captain, name in ((self.captain, "New"), (self.other, " FIRST ")):
            with self.assertRaises(ValidationError):
                self.team(captain, name)
        for actor in (AnonymousUser(), None):
            with self.assertRaises(PermissionDenied):
                create_team(self.tournament, actor, "Other", "@valid_name", "discord")
        for consent in (False, "true", 1):
            with self.assertRaises(ValidationError):
                create_team(self.tournament, self.other, "Other", "@valid_name", "discord", accept_rules=consent)
        for telegram, discord in (("+77001234567", "discord"), ("name@example.com", "discord"), ("@valid_name", "name@example.com"), ("@valid_name", "+77001234567"), ("@valid_name\nInjected", "discord")):
            with self.assertRaises(ValidationError):
                create_team(self.tournament, self.other, "Other", telegram, discord)
        self.assertEqual(Team.objects.count(), 1)

    def test_telegram_and_active_user_required_from_current_database(self):
        self.captain.telegram_identity.delete()
        with self.assertRaises(PermissionDenied):
            self.team()
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.team(self.other)

    def test_existing_player_cannot_create_a_different_captain_team(self):
        first = self.team()
        user = self.user("player", "123")
        join_team(first, user)
        with self.assertRaises(ValidationError):
            self.team(user, "Second")
        self.assertFalse(Team.objects.filter(captain=user).exists())
        self.assertEqual(Participant.objects.filter(account=user).count(), 1)

    def test_captain_can_join_own_team_but_not_another(self):
        own = self.team()
        other = self.team(self.other, "Other")
        GameAccount.objects.create(user=self.captain, game_id="123", server_id="45", nickname="Captain")
        with self.assertRaises(ValidationError):
            join_team(other, self.captain)
        self.assertFalse(other.participants.exists())
        joined = join_team(own, self.captain)
        self.assertEqual(joined.team, own)
        self.assertEqual(joined.account, self.captain)
        self.assertTrue(joined.registration_confirmed)

    def test_join_uses_own_profile_and_payment_never_confirmed(self):
        team = self.team()
        player = self.user("player", "123")
        participant = join_team(team, player)
        self.assertEqual(participant.nickname, "Nick player")
        self.assertEqual(participant.account, player)
        self.assertTrue(participant.registration_confirmed)
        self.assertEqual(participant.payment.amount, 2200)
        self.assertEqual(participant.payment.status, Payment.Status.AWAITING)
        again = join_team(team, player, role="reserve")
        self.assertEqual(again.pk, participant.pk)
        self.assertFalse(again.is_reserve)
        self.assertEqual(Payment.objects.count(), 1)
        with self.assertRaises(ValidationError):
            join_team(team, self.other)

    def test_slots_reserve_fee_and_invalid_roles(self):
        team = self.team()
        first, second, third = [self.user(f"p{i}", str(100 + i)) for i in range(3)]
        join_team(team, first)
        with self.assertRaises(ValidationError):
            join_team(team, second)
        reserve = join_team(team, second, role="reserve")
        self.assertTrue(reserve.is_reserve)
        self.assertFalse(reserve.payment.required)
        self.assertEqual(reserve.payment.amount, 0)
        with self.assertRaises(ValidationError):
            join_team(team, third, role="reserve")
        for role in ("captain", "MAIN", True, None):
            with self.assertRaises(ValidationError):
                join_team(team, third, role=role)

    def test_unknown_reserves_not_invented_and_paid_reserve_snapshot(self):
        self.tournament.reserve_limit = None
        self.tournament.reserves_pay = None
        self.tournament.save()
        team = self.team()
        user = self.user("reserve", "123")
        with self.assertRaises(ValidationError):
            join_team(team, user, role="reserve")
        self.tournament.reserve_limit = 1
        self.tournament.reserves_pay = True
        self.tournament.save()
        row = join_team(team, user, role="reserve")
        self.assertEqual((row.payment.required, row.payment.amount), (True, 2200))

    def test_unknown_rules_can_be_completed_after_draft_main_joins_then_submitted(self):
        self.tournament.reserve_limit = None
        self.tournament.reserves_pay = None
        self.tournament.terms_confirmed = False
        self.tournament.registration_open = False
        self.tournament.save()
        team = self.team()
        row = join_team(team, self.user("player", "123"))
        with self.assertRaises(ValidationError):
            submit_team(team, self.captain)
        self.tournament.reserve_limit = 1
        self.tournament.reserves_pay = False
        self.tournament.terms_confirmed = True
        self.tournament.registration_open = True
        self.tournament.save()
        self.assertEqual(submit_team(team, self.captain).status, Team.Status.PENDING)
        row.payment.refresh_from_db()
        self.assertEqual((row.payment.required, row.payment.amount), (True, 2200))

    def test_draft_limits_cannot_shrink_below_actual_roster_or_reset_unknown(self):
        self.tournament.roster_size = 2
        self.tournament.reserve_limit = 2
        self.tournament.save()
        team = self.team()
        for number, role in ((1, "main"), (2, "main"), (3, "reserve"), (4, "reserve")):
            join_team(team, self.user(f"p{number}", str(100 + number)), role=role)
        for field, value in (("roster_size", 1), ("reserve_limit", 1), ("reserve_limit", None), ("reserves_pay", None), ("reserves_pay", True)):
            with self.subTest(field=field, value=value):
                self.tournament.refresh_from_db()
                setattr(self.tournament, field, value)
                with self.assertRaises(ValidationError):
                    self.tournament.save()
        self.tournament.refresh_from_db()
        self.tournament.roster_size = 3
        self.tournament.reserve_limit = 3
        self.tournament.save()
        self.assertTrue(all(row.payment.amount == 0 for row in team.participants.filter(is_reserve=True)))

    def test_pending_or_financial_history_freezes_configuration_even_after_reopen(self):
        team = self.team()
        row = join_team(team, self.user("player", "123"))
        submit_team(team, self.captain)
        self.tournament.roster_size = 2
        with self.assertRaises(ValidationError):
            self.tournament.save()
        submit_payment(row)
        Team.objects.filter(pk=team.pk).update(status=Team.Status.DRAFT)
        with self.assertRaises(ValidationError):
            self.tournament.save()
        # Even a legacy row returned to AWAITING cannot erase audit/outbox evidence.
        Payment.objects.filter(participant=row).update(status=Payment.Status.AWAITING, submitted_at=None)
        with self.assertRaises(ValidationError):
            self.tournament.save()

    def test_duplicates_blocked_across_teams_and_legacy_zeroes(self):
        first = self.team()
        second = self.team(self.other, "Second")
        reserved = Participant.objects.create(team=first, nickname="Legacy", game_id="00123", server_id="045")
        user = self.user("player", "123")
        with self.assertRaises(ValidationError):
            join_team(second, user)
        with patch("arena.team_services.game_lookup.lookup", return_value=LookupResult("Name", "gopay", "123", "45")):
            with self.assertRaises(ValidationError):
                add_player(second, self.other, "123", "45")
        own = join_team(first, user)
        self.assertEqual(own.pk, reserved.pk)
        self.assertEqual(submission_errors(first), [])

    def test_one_user_cannot_claim_two_memberships_even_if_legacy_data_conflicts(self):
        first = self.team()
        second = self.team(self.other, "Second")
        user = self.user("player", "123")
        Participant.objects.create(team=first, account=user, nickname="Legacy", game_id="999", server_id="45")
        with self.assertRaises(ValidationError):
            join_team(second, user)
        self.assertEqual(Participant.objects.count(), 1)

    def test_pending_can_claim_reserved_pair_preserves_role_and_payment(self):
        team = self.team()
        reserved = self.reserve(team, role="reserve")
        original_payment = reserved.payment.pk
        Team.objects.filter(pk=team.pk).update(status=Team.Status.PENDING)
        user = self.user("player", "123")
        claimed = join_team(team, user)
        self.assertEqual(claimed.pk, reserved.pk)
        self.assertTrue(claimed.is_reserve)
        self.assertTrue(claimed.registration_confirmed)
        self.assertEqual(claimed.nickname, "Nick player")
        self.assertEqual(claimed.payment.pk, original_payment)
        with self.assertRaises(ValidationError):
            join_team(team, self.user("new", "456"))

    def test_foreign_claim_and_closed_teams_never_mutate(self):
        team = self.team()
        user = self.user("player", "123")
        row = self.reserve(team)
        row.account = self.other
        row.save(update_fields=["account"])
        with self.assertRaises(PermissionDenied):
            join_team(team, user)
        for status in (Team.Status.CONFIRMED, Team.Status.REJECTED):
            Team.objects.filter(pk=team.pk).update(status=status)
            with self.assertRaises(ValidationError):
                join_team(team, user)
        row.refresh_from_db()
        self.assertFalse(row.registration_confirmed)

    def test_bracket_closes_all_roster_mutations(self):
        team = self.team()
        Match.objects.create(tournament=self.tournament, key="W1", bracket="upper", round=1, index=1, best_of=1)
        with self.assertRaises(ValidationError):
            join_team(team, self.user("player", "123"))
        with self.assertRaises(ValidationError):
            self.team(self.other, "Other")
        with patch("arena.team_services.game_lookup.lookup") as lookup:
            with self.assertRaises(ValidationError):
                add_player(team, self.captain, "123", "45")
            lookup.assert_not_called()

    def test_add_player_uses_api_nickname_and_waits_for_personal_consent(self):
        team = self.team()
        player = self.reserve(team)
        self.assertEqual(player.nickname, "API Nick")
        self.assertIsNone(player.account_id)
        self.assertFalse(player.registration_confirmed)
        self.assertEqual(self.reserve(team).pk, player.pk)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertTrue(submission_errors(team))

    @patch("arena.team_services.game_lookup.lookup")
    def test_unauthorized_add_does_not_send_ids_and_api_failure_leaves_no_row(self, lookup):
        team = self.team()
        with self.assertRaises(PermissionDenied):
            add_player(team, self.other, "123", "45")
        lookup.assert_not_called()
        lookup.side_effect = LookupUnavailable("Temporary")
        with self.assertRaises(LookupUnavailable):
            add_player(team, self.captain, "123", "45")
        self.assertFalse(team.participants.exists())
        self.assertFalse(Payment.objects.exists())

    def test_add_revalidates_status_after_network(self):
        team = self.team()
        def lookup(*args):
            Team.objects.filter(pk=team.pk).update(status=Team.Status.PENDING)
            return LookupResult("Name", "gopay", "123", "45")
        with patch("arena.team_services.game_lookup.lookup", side_effect=lookup):
            with self.assertRaises(ValidationError):
                add_player(team, self.captain, "123", "45")
        self.assertFalse(Participant.objects.exists())

    def test_submission_requires_consented_bound_active_roster_but_not_payment(self):
        team = self.team()
        with self.assertRaises(ValidationError):
            submit_team(team, self.captain)
        row = self.reserve(team)
        with self.assertRaises(ValidationError):
            submit_team(team, self.captain)
        player = self.user("player", "123")
        join_team(team, player)
        self.assertEqual(submission_errors(team), [])
        with self.assertRaises(PermissionDenied):
            submit_team(team, self.other)
        submitted = submit_team(team, self.captain)
        self.assertEqual(submitted.status, Team.Status.PENDING)
        self.assertIsNotNone(submitted.submitted_at)
        self.assertEqual(row.payment.status, Payment.Status.AWAITING)
        self.assertEqual(submit_team(team, self.captain).submitted_at, submitted.submitted_at)

    def test_submission_closed_registration_and_stale_telegram_block(self):
        team = self.team()
        user = self.user("player", "123")
        join_team(team, user)
        self.tournament.registration_open = False
        self.tournament.save()
        with self.assertRaises(ValidationError):
            submit_team(team, self.captain)
        self.tournament.registration_open = True
        self.tournament.save()
        user.telegram_identity.delete()
        self.assertTrue(submission_errors(team))
        with self.assertRaises(ValidationError):
            submit_team(team, self.captain)

    def test_drafts_do_not_consume_submission_capacity(self):
        first = self.team()
        second = self.team(self.other, "Second")
        third = self.team(self.user("thirdcaptain"), "Third")
        join_team(first, self.user("p1", "123"))
        join_team(second, self.user("p2", "124"))
        join_team(third, self.user("p3", "125"))
        submit_team(first, self.captain)
        submit_team(second, self.other)
        with self.assertRaises(ValidationError):
            submit_team(third, third.captain)
        self.assertEqual(Team.objects.filter(status=Team.Status.PENDING).count(), 2)

    def test_remove_unpaid_reserved_seat_and_self_leave(self):
        team = self.team()
        reserved = self.reserve(team)
        remove_player(team, self.captain, reserved.pk)
        self.assertFalse(Participant.objects.exists())
        self.assertFalse(Payment.objects.exists())
        user = self.user("player", "124")
        joined = join_team(team, user)
        remove_player(team, user, joined.pk)
        self.assertFalse(Participant.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_remove_cannot_cross_team_or_impersonate_captain(self):
        team = self.team()
        row = self.reserve(team)
        other_team = self.team(self.other, "Other")
        with self.assertRaises(PermissionDenied):
            remove_player(team, self.other, row.pk)
        with self.assertRaises(ValidationError):
            remove_player(other_team, self.other, row.pk)
        Team.objects.filter(pk=team.pk).update(status=Team.Status.PENDING)
        with self.assertRaises(ValidationError):
            remove_player(team, self.captain, row.pk)
        self.assertTrue(Participant.objects.filter(pk=row.pk).exists())

    def test_remove_preserves_financial_states_and_evidence(self):
        team = self.team()
        row = self.reserve(team)
        payment = row.payment
        for fields in ({"status": Payment.Status.REVIEW}, {"status": Payment.Status.REJECTED}, {"bank_reference": "bank"}, {"submitted_at": timezone.now()}):
            Payment.objects.filter(pk=payment.pk).update(**fields)
            with self.assertRaises(ValidationError):
                remove_player(team, self.captain, row.pk)
            Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.AWAITING, bank_reference=None, submitted_at=None)
        Outbox.objects.create(payment=payment, kind="review")
        with self.assertRaises(ValidationError):
            remove_player(team, self.captain, row.pk)
        Outbox.objects.all().delete()
        session = TelegramReviewSession.objects.create(payment=payment, submitted_at_snapshot=timezone.now(), action="approve", chat_id=1, user_id=1, expires_at=timezone.now())
        with self.assertRaises(ValidationError):
            remove_player(team, self.captain, row.pk)
        session.delete()
        PaymentAudit.objects.create(payment=payment, from_status="awaiting", to_status="review")
        with self.assertRaises(ValidationError):
            remove_player(team, self.captain, row.pk)
        self.assertTrue(Participant.objects.filter(pk=row.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())


class TeamTransactionTests(Fixtures, TransactionTestCase):
    def test_migration_assigns_distinct_invites_to_existing_teams(self):
        before = [("arena", "0003_telegram_auth_game_accounts")]
        after = [("arena", "0004_team_draft_invitation")]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            LegacyTeam = executor.loader.project_state(before).apps.get_model("arena", "Team")
            ids = [LegacyTeam.objects.create(tournament_id=self.tournament.pk, captain_id=user.pk,
                                            name=user.username, contact="@contact").pk
                   for user in (self.captain, self.other)]
            MigrationExecutor(connection).migrate(after)
            restored = list(Team.objects.filter(pk__in=ids).order_by("pk"))
            self.assertEqual(len({team.invite_token for team in restored}), 2)
            self.assertTrue(all(team.invite_token and team.status == Team.Status.PENDING
                                and team.submitted_at is None for team in restored))
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())

    def test_http_happens_before_database_transaction_and_revalidates_slots(self):
        team = self.team()
        competitor = self.user("competitor", "124")
        def lookup(*args):
            self.assertFalse(connection.in_atomic_block)
            join_team(team, competitor)
            return LookupResult("Name", "gopay", "123", "45")
        with patch("arena.team_services.game_lookup.lookup", side_effect=lookup):
            with self.assertRaises(ValidationError):
                add_player(team, self.captain, "123", "45")
        self.assertEqual(team.participants.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)

    def test_concurrent_last_slot_never_overfills(self):
        team = self.team()
        players = [self.user("p1", "123"), self.user("p2", "124")]
        barrier = Barrier(2)
        def join(user):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return ("joined", join_team(team, user).pk)
            except (ValidationError, OperationalError) as exc:
                # SQLite shared-memory test DB reports a lock immediately;
                # production file DB uses IMMEDIATE + configured busy timeout.
                return ("blocked", type(exc).__name__)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(join, players))
        self.assertEqual(sum(result[0] == "joined" for result in results), 1, results)
        self.assertEqual(team.participants.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
