from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from arena.models import Match, Outbox, Participant, Payment, PaymentAudit, Team, TelegramIdentity, Tournament
from arena.services import check_roster, confirm_registration, confirm_team, generate_bracket, record_result, register_team, review_payment, submit_payment


@override_settings(TELEGRAM_CASHIER_ID=855341070)
class DomainTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create(username="organizer", is_staff=True)
        self.captain = get_user_model().objects.create(username="captain")
        start = timezone.now() + timedelta(days=2)
        self.tournament = Tournament.objects.create(
            registration_open=True, terms_confirmed=True, rules="Подтверждённые правила",
            timezone_name="UTC", event_date=start.date(), starts_at=start,
            end_registration=start - timedelta(hours=1), max_teams=4, roster_size=1,
            reserve_limit=1, reserves_pay=True, best_of=3, final_best_of=5,
            grand_final_reset=True,
        )

    def register(self, number=1, reserve=False, captain=None):
        captain = captain or get_user_model().objects.create(username=f"captain-{number}")
        players = [{"nickname": f"Player {number}", "game_id": str(number), "server_id": "123", "is_reserve": False}]
        if reserve:
            players.append({"nickname": f"Reserve {number}", "game_id": f"r{number}", "server_id": "123", "is_reserve": True})
        team = register_team(self.tournament, captain, f"Team {number}", "@captain", players)
        for participant in team.participants.all():
            account = get_user_model().objects.create(username=f"participant-{participant.pk}")
            TelegramIdentity.objects.create(user=account, telegram_id=1000000 + participant.pk)
            participant.account = account
            participant.save(update_fields=["account"])
        return team

    def ready(self, number):
        team = self.register(number)
        participant = team.participants.get()
        confirm_registration(participant)
        payment = submit_payment(participant)
        review_payment(payment.pk, True, bank_reference=f"bank-{number}", actor_user=self.staff)
        check_roster(team, self.staff)
        return confirm_team(team, self.staff)

    def test_draft_blocks_registration_and_creates_nothing(self):
        self.tournament.terms_confirmed = False
        self.tournament.save()
        with self.assertRaises(ValidationError):
            self.register(captain=self.captain)
        self.assertFalse(Team.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_unknown_times_and_invalid_date_block_registration(self):
        self.tournament.starts_at = None
        self.tournament.save()
        with self.assertRaises(ValidationError):
            self.register(captain=self.captain)
        self.tournament.starts_at = timezone.now() + timedelta(days=3)
        with self.assertRaises(ValidationError):
            self.tournament.save()

    def test_duplicate_player_and_captain_rejected_atomically(self):
        first = self.register(captain=self.captain)
        with self.assertRaises(ValidationError):
            self.register(2, captain=self.captain)
        other = get_user_model().objects.create(username="other")
        with self.assertRaises(ValidationError):
            self.register(1, captain=other)
        self.assertEqual(Team.objects.count(), 1)
        self.assertEqual(Participant.objects.count(), 1)
        self.assertEqual(first.participants.get().tournament_id, self.tournament.pk)

    def test_registration_capacity_and_server_validated_roster(self):
        for i in range(1, 5):
            self.register(i)
        with self.assertRaises(ValidationError):
            self.register(5)
        self.assertEqual(Team.objects.count(), 4)

    def test_payment_requires_participation_and_never_auto_confirms(self):
        participant = self.register().participants.get()
        with self.assertRaises(ValidationError):
            submit_payment(participant)
        confirm_registration(participant)
        payment = submit_payment(participant)
        self.assertEqual(payment.status, "review")
        self.assertEqual(payment.amount, 2200)
        self.assertEqual(payment.code, f"CQC-{payment.pk:06d}")
        submit_payment(participant)
        self.assertEqual(Outbox.objects.filter(kind="review").count(), 1)
        with self.assertRaises(PermissionDenied):
            review_payment(payment.pk, True, "receipt", actor_user=self.captain)
        with self.assertRaises(PermissionDenied):
            review_payment(payment.pk, True, "receipt", actor_telegram=42)
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, True, actor_user=self.staff)
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, True, payment.code, actor_user=self.staff)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "review")

    def test_payment_rejection_resubmission_and_unique_bank_reference(self):
        first = self.register(1).participants.get()
        second = self.register(2).participants.get()
        for participant in (first, second):
            confirm_registration(participant)
            submit_payment(participant)
        payment = first.payment
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, False, actor_telegram=855341070)
        review_payment(payment.pk, False, reason="Выписка не содержит платёж", actor_telegram=855341070)
        resubmitted = submit_payment(first)
        self.assertIsNone(resubmitted.reviewed_at)
        self.assertIsNone(resubmitted.reviewed_by_telegram)
        confirmed = review_payment(payment.pk, True, " receipt-001 ", actor_telegram=855341070)
        self.assertEqual(confirmed.bank_reference, "RECEIPT-001")
        self.assertEqual(confirmed.reviewed_by_telegram, 855341070)
        self.assertIsNotNone(confirmed.reviewed_at)
        with self.assertRaises(ValidationError):
            submit_payment(first)
        with self.assertRaises(ValidationError):
            review_payment(second.payment.pk, True, "receipt-001", actor_user=self.staff)
        second.payment.refresh_from_db()
        self.assertEqual(second.payment.status, "review")
        self.assertEqual(PaymentAudit.objects.filter(payment=payment).count(), 4)
        audit = PaymentAudit.objects.filter(payment=payment).last()
        with self.assertRaises(ValidationError):
            audit.save()
        with self.assertRaises(ValidationError):
            audit.delete()
        with self.assertRaises(ValidationError):
            PaymentAudit.objects.filter(pk=audit.pk).update(reason="changed")
        with self.assertRaises(ValidationError):
            PaymentAudit.objects.filter(pk=audit.pk).delete()

    def test_payment_fee_database_constraint(self):
        payment = self.register().participants.get().payment
        with self.assertRaises(IntegrityError), transaction.atomic():
            Payment.objects.filter(pk=payment.pk).update(amount=1)

    def test_free_reserve_requires_consent_but_not_payment(self):
        self.tournament.reserves_pay = False
        self.tournament.save()
        team = self.register(reserve=True)
        primary = team.participants.get(is_reserve=False)
        reserve = team.participants.get(is_reserve=True)
        self.assertFalse(reserve.payment.required)
        self.assertEqual(reserve.payment.amount, 0)
        confirm_registration(primary)
        submit_payment(primary)
        review_payment(primary.payment.pk, True, "ref", actor_user=self.staff)
        with self.assertRaises(ValidationError):
            check_roster(team, self.staff)
        confirm_registration(reserve)
        with self.assertRaises(ValidationError):
            submit_payment(reserve)
        check_roster(team, self.staff)
        self.assertEqual(confirm_team(team, self.staff).status, "confirmed")

    def test_readiness_and_staff_required_for_admission(self):
        team = self.register()
        with self.assertRaises(PermissionDenied):
            check_roster(team, self.captain)
        with self.assertRaises(ValidationError):
            confirm_team(team, self.staff)
        confirm_registration(team.participants.get())
        check_roster(team, self.staff)
        with self.assertRaises(ValidationError):
            confirm_team(team, self.staff)

    def test_legacy_confirmation_and_payment_do_not_replace_telegram_link(self):
        team = self.register()
        participant = team.participants.get()
        confirm_registration(participant)
        payment = submit_payment(participant)
        review_payment(payment.pk, True, "LEGACY-BANK-1", actor_user=self.staff)
        check_roster(team, self.staff)
        # A pre-Telegram registration may retain consent, payment and an older
        # roster check while the new account column remains NULL.
        participant.account = None
        participant.save(update_fields=["account"])
        participant.refresh_from_db()
        self.assertTrue(participant.registration_confirmed)
        self.assertEqual(participant.payment.status, "confirmed")
        with self.assertRaises(ValidationError):
            check_roster(team, self.staff)
        with self.assertRaises(ValidationError):
            confirm_team(team, self.staff)

    def test_roster_configuration_locked_after_registration(self):
        self.register()
        self.tournament.reserves_pay = False
        with self.assertRaises(ValidationError):
            self.tournament.save()

    def test_configuration_validation_on_save(self):
        cases = [
            ("kaspi_url", "https://example.com/pay/test"),
            ("kaspi_url", "https://user@pay.kaspi.kz/pay/test"),
            ("kaspi_url", "http://pay.kaspi.kz/pay/test"),
            ("kaspi_url", "https://pay.kaspi.kz:444/pay/test"),
            ("max_teams", 3), ("max_teams", 128), ("roster_size", 0),
            ("roster_size", 11), ("reserve_limit", 11), ("best_of", 2),
            ("final_best_of", 11), ("timezone_name", "Invalid/Zone"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                tournament = Tournament.objects.get(pk=self.tournament.pk)
                setattr(tournament, field, value)
                with self.assertRaises(ValidationError):
                    tournament.save()

    def test_team_names_cannot_differ_only_in_case(self):
        self.register(1)
        players = [{"nickname": "Other", "game_id": "different", "server_id": "123"}]
        with self.assertRaises(ValidationError):
            register_team(self.tournament, self.captain, "team 1", "@captain", players)

    def test_incomplete_bracket_blocked(self):
        self.ready(1)
        with self.assertRaises(ValidationError):
            generate_bracket(self.tournament, self.staff)
        self.assertFalse(Match.objects.exists())

    def simulate(self, count, reset):
        self.tournament.max_teams = count
        self.tournament.save()
        for i in range(1, count + 1):
            self.ready(i)
        matches = generate_bracket(self.tournament, self.staff)
        self.assertEqual(len(matches), 2 * count - 1)
        with self.assertRaises(ValidationError):
            generate_bracket(self.tournament, self.staff)
        self.tournament.refresh_from_db()
        self.tournament.registration_open = True
        with self.assertRaises(ValidationError):
            self.tournament.save()
        self.tournament.refresh_from_db()
        losses = Counter()
        while True:
            available = Match.objects.filter(active=True, completed_at__isnull=True, team_a__isnull=False, team_b__isnull=False).first()
            if available is None:
                break
            self.assertNotEqual(available.team_a_id, available.team_b_id)
            self.assertLess(losses[available.team_a_id], 2)
            self.assertLess(losses[available.team_b_id], 2)
            win_score = available.best_of // 2 + 1
            score_a, score_b = (0, win_score) if reset and available.bracket == "final" else (win_score, 0)
            result = record_result(available, score_a, score_b, self.staff)
            losses[result.loser.pk] += 1
        reset_match = Match.objects.get(key="F2")
        self.assertEqual(reset_match.active, reset)
        self.assertEqual(reset_match.completed_at is not None, reset)
        self.assertFalse(Match.objects.filter(active=True, completed_at__isnull=True).exists())
        champion = reset_match.winner if reset else Match.objects.get(key="F1").winner
        self.assertLess(losses[champion.pk], 2)
        self.assertEqual(sum(value == 2 for value in losses.values()), count - 1)
        return champion

    def test_four_team_double_elimination_without_reset(self):
        self.simulate(4, reset=False)

    def test_sixteen_team_double_elimination_with_reset(self):
        self.simulate(16, reset=True)

    def test_two_team_format_without_reset_configuration(self):
        self.tournament.max_teams = 2
        self.tournament.grand_final_reset = False
        self.tournament.save()
        self.ready(1)
        self.ready(2)
        matches = generate_bracket(self.tournament, self.staff)
        self.assertEqual(len(matches), 2)
        upper = record_result(Match.objects.get(key="W1-1"), 2, 0, self.staff)
        final = Match.objects.get(key="F1")
        self.assertEqual(final.team_a_id, upper.winner_id)
        self.assertEqual(final.team_b_id, upper.loser.pk)
        record_result(final, 0, 3, self.staff)
        self.assertFalse(Match.objects.filter(active=True, completed_at__isnull=True).exists())

    def test_result_validates_score_actor_dependencies_and_immutability(self):
        for i in range(1, 5):
            self.ready(i)
        generate_bracket(self.tournament, self.staff)
        first = Match.objects.get(key="W1-1")
        with self.assertRaises(PermissionDenied):
            record_result(first, 2, 0, self.captain)
        for scores in [(1, 0), (2, 2), (3, 0), (-1, 2), (True, 0)]:
            with self.assertRaises(ValidationError):
                record_result(first, *scores, self.staff)
        with self.assertRaises(ValidationError):
            record_result(Match.objects.get(key="W2-1"), 2, 0, self.staff)
        record_result(first, 2, 1, self.staff)
        with self.assertRaises(ValidationError):
            record_result(first, 0, 2, self.staff)
        following = Match.objects.get(key="W2-1")
        self.assertEqual(following.team_a_id, first.team_a_id)
        self.assertIsNone(following.team_b_id)
