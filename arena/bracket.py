from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Match, Team, Tournament
from .services import _staff, team_readiness


def _source_outcome(source, outcome):
    """Only call after the source is resolved; None means a known empty slot."""
    if outcome == "winner":
        return source.winner_id
    if outcome != "loser":
        raise ValidationError("Некорректная связь матчей в сетке.")
    if source.resolution in (Match.Resolution.BYE, Match.Resolution.EMPTY):
        return None
    if not source.winner_id:
        return None
    return source.team_b_id if source.winner_id == source.team_a_id else source.team_a_id


def _advance(tournament, actor_user=None):
    """Resolve known empty branches, never treating an unresolved source as empty.

    Generation inserts every source before its consumers. This topological
    pass advances both played results and arbitrarily many consecutive byes.
    Tournament locking in callers serializes concurrent results.
    """
    matches = list(Match.objects.select_for_update().filter(tournament=tournament).order_by("pk"))
    by_id = {match.pk: match for match in matches}
    for match in matches:
        if match.completed_at:
            continue
        changed = []
        if actor_user is not None:
            from .match_services import _audit, _notify, _snapshot
            before = _snapshot(match)
        waiting = False
        for side in ("a", "b"):
            source_id = getattr(match, f"source_{side}_id")
            if source_id is None:
                continue
            source = by_id[source_id]
            if not source.completed_at:
                waiting = True
                continue
            expected = _source_outcome(source, getattr(match, f"source_{side}_outcome"))
            if getattr(match, f"team_{side}_id") != expected:
                setattr(match, f"team_{side}_id", expected)
                changed.append(f"team_{side}")
        if match.bracket == Match.Bracket.RESET:
            final = by_id[match.source_a_id]
            active = bool(final.completed_at and final.resolution in (Match.Resolution.PLAYED, Match.Resolution.FORFEIT)
                          and final.winner_id == final.team_b_id)
            if match.active != active:
                match.active = active
                changed.append("active")
        if match.active and not waiting:
            present = [team_id for team_id in (match.team_a_id, match.team_b_id) if team_id is not None]
            if len(present) == 2 and present[0] == present[1]:
                raise ValidationError("Ошибка сетки: команда не может играть сама с собой.")
            if len(present) < 2:
                match.resolution = Match.Resolution.BYE if present else Match.Resolution.EMPTY
                match.winner_id = present[0] if present else None
                match.completed_at = timezone.now()
                # A bye is advancement, not a scored victory or a team loss.
                match.score_a = match.score_b = None
                changed.extend(["resolution", "winner", "completed_at", "score_a", "score_b"])
        if changed:
            match.save(update_fields=changed)
            if actor_user is not None:
                audit = _audit(match, actor_user, "advancement", before)
                _notify(match, audit, "Обновлены участники следующего матча")


def _seed_slots(teams, size):
    """Balanced seeding: every first-round empty slot faces an actual team."""
    seeds = [1, 2]
    while len(seeds) < size:
        complement = len(seeds) * 2 + 1
        seeds = [value for seed in seeds for value in (seed, complement - seed)]
    return [teams[seed - 1] if seed <= len(teams) else None for seed in seeds]


@transaction.atomic
def generate_bracket(tournament, actor_user):
    """Build deterministic double elimination for any 2..capacity teams."""
    _staff(actor_user)
    tournament = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if tournament.matches.exists():
        raise ValidationError("Сетка уже сформирована; повторная генерация запрещена.")
    if not tournament.terms_confirmed:
        raise ValidationError("Сначала подтвердите условия турнира.")
    if tournament.grand_final_reset is None or any(value is None or value < 1 or value % 2 != 1 for value in (tournament.best_of, tournament.final_best_of)):
        raise ValidationError("Укажите формат матчей, финала и правило повторного финала.")
    teams = list(tournament.teams.filter(status=Team.Status.CONFIRMED).order_by("created_at", "pk"))
    count = len(teams)
    if count < 2 or count > tournament.max_teams:
        raise ValidationError(f"Для формирования сетки нужны от 2 до {tournament.max_teams} допущенных команд.")
    size = 1 << (count - 1).bit_length()
    for team in teams:
        errors = team_readiness(team)
        if errors:
            raise ValidationError([f"{team.name}: {error}" for error in errors])

    def make(key, bracket, round_number, index, source_a=None, outcome_a="winner", source_b=None, outcome_b="winner", **kwargs):
        return Match.objects.create(
            tournament=tournament, key=key, bracket=bracket, round=round_number, index=index,
            source_a=source_a, source_a_outcome=outcome_a if source_a else "",
            source_b=source_b, source_b_outcome=outcome_b if source_b else "",
            best_of=tournament.final_best_of if bracket in (Match.Bracket.FINAL, Match.Bracket.RESET) else tournament.best_of,
            **kwargs,
        )

    upper = {}
    slots = _seed_slots(teams, size)
    upper[1] = [make(f"W1-{i + 1}", Match.Bracket.UPPER, 1, i + 1, team_a=slots[2 * i], team_b=slots[2 * i + 1]) for i in range(size // 2)]
    round_number = 2
    while len(upper[round_number - 1]) > 1:
        previous = upper[round_number - 1]
        upper[round_number] = [make(f"W{round_number}-{i + 1}", Match.Bracket.UPPER, round_number, i + 1, previous[2 * i], "winner", previous[2 * i + 1]) for i in range(len(previous) // 2)]
        round_number += 1

    if size == 2:
        lower_final = upper[1][0]
        lower_outcome = "loser"
    else:
        previous = [make(f"L1-{i + 1}", Match.Bracket.LOWER, 1, i + 1, upper[1][2 * i], "loser", upper[1][2 * i + 1], "loser") for i in range(size // 4)]
        for upper_round in range(2, len(upper) + 1):
            lower_round = 2 * upper_round - 2
            # Cross adjacent lower-bracket branches to avoid immediate rematches.
            previous = [make(f"L{lower_round}-{i + 1}", Match.Bracket.LOWER, lower_round, i + 1, previous[i ^ 1] if len(previous) > 1 else previous[i], "winner", match, "loser") for i, match in enumerate(upper[upper_round])]
            if len(previous) > 1:
                lower_round += 1
                previous = [make(f"L{lower_round}-{i + 1}", Match.Bracket.LOWER, lower_round, i + 1, previous[2 * i], "winner", previous[2 * i + 1]) for i in range(len(previous) // 2)]
        lower_final, lower_outcome = previous[0], "winner"
    final = make("F1", Match.Bracket.FINAL, 1, 1, upper[len(upper)][0], "winner", lower_final, lower_outcome)
    if tournament.grand_final_reset:
        make("F2", Match.Bracket.RESET, 2, 1, final, "winner", final, "loser", active=False)
    _advance(tournament, actor_user)
    tournament.registration_open = False
    tournament.save(update_fields=["registration_open"])
    return list(tournament.matches.all())


@transaction.atomic
def record_result(match, score_a, score_b, actor_user):
    from .match_services import _audit, _fresh_staff, _lock, _notify, _snapshot
    _staff(actor_user)
    # Serialize all advancement in a tournament, including a grand-final reset.
    match = _lock(match)
    _fresh_staff(actor_user)
    before = _snapshot(match)
    if match.completed_at or match.winner_id:
        raise ValidationError("Результат уже записан. Перезапись нарушит зависимые матчи.")
    if not match.active or not match.team_a_id or not match.team_b_id:
        raise ValidationError("Матч ещё не готов к проведению.")
    if match.team_a_id == match.team_b_id:
        raise ValidationError("Команда не может играть сама с собой.")
    for side in ("a", "b"):
        source = getattr(match, f"source_{side}")
        if source:
            if not source.completed_at:
                raise ValidationError("Сначала завершите предыдущие матчи.")
            expected = _source_outcome(source, getattr(match, f"source_{side}_outcome"))
            if getattr(match, f"team_{side}_id") != expected:
                raise ValidationError("Участники матча не соответствуют предыдущим результатам.")
    if any(isinstance(score, bool) or not isinstance(score, int) or score < 0 for score in (score_a, score_b)):
        raise ValidationError("Счёт должен состоять из неотрицательных целых чисел.")
    required = match.best_of // 2 + 1
    if match.best_of < 1 or match.best_of % 2 != 1 or not ((score_a == required and score_b < required) or (score_b == required and score_a < required)):
        raise ValidationError(f"Для BO{match.best_of} победитель должен выиграть ровно {required} карт.")
    match.score_a, match.score_b = score_a, score_b
    match.winner_id = match.team_a_id if score_a > score_b else match.team_b_id
    match.completed_at = timezone.now()
    match.resolution = Match.Resolution.PLAYED
    match.save(update_fields=["score_a", "score_b", "winner", "completed_at", "resolution"])
    _advance(match.tournament_id, actor_user)
    audit = _audit(match, actor_user, "result", before)
    _notify(match, audit, "Опубликован результат матча")
    return match
