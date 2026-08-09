"""
Phase 1 test suite.

Covers the two ROADMAP Phase 1 acceptance criteria explicitly, plus the
rating math and the quirk handling that took real investigation to get
right. ROADMAP standing rule 3 asks for tests on the rating math
specifically, "where correctness bugs hide and they're cheap to cover".

The reconciliation values asserted below are not invented — they come from
the Phase 0 manual check against Basketball-Reference (see MEMORY.md). That
makes these regression tests against a published source rather than against
our own previous output, which is the difference between "still the same"
and "still correct".
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from ingest import ratings
from ingest.models import PlayerBoxRow, TeamBoxRow
from ingest.orchestrator import run_pipeline
from ingest.providers import FixtureProvider, ProviderError, get_provider
from ingest.stages import normalize, publish, transactions, validate
from ingest.stages.validate import BoundaryValidationError
from ingest.state import PipelineState

SEASON = 2026
AS_OF = date(2026, 8, 7)
FIXTURES = Path("fixtures")

pytestmark = pytest.mark.skipif(
    not (FIXTURES / f"player_box_{SEASON}.parquet").exists(),
    reason="fixtures absent; run `python -m ingest --provider hoopr` once",
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def normalized_state() -> PipelineState:
    """State through the normalize stage — the input most stages expect."""
    box = FixtureProvider(FIXTURES).get_box_scores(SEASON)
    state = PipelineState(
        season=SEASON,
        provider_name="fixture",
        as_of_date=AS_OF,
        raw_player_box=box.player_box,
        raw_team_box=box.team_box,
        # Required for the NBA Cup Championship exclusion — without it the
        # stage silently degrades to keeping the game, so omitting it here
        # would make these tests disagree with the real pipeline.
        raw_schedule=box.schedule,
    )
    return normalize.run(state)


@pytest.fixture(scope="module")
def rated_state(normalized_state: PipelineState) -> PipelineState:
    from ingest.stages import aggregate, qa

    state = transactions.run(normalized_state)
    state = aggregate.run(state)
    return qa.run(state)


# --------------------------------------------------------------------------
# ACCEPTANCE 1 — running ingest twice produces identical state
# --------------------------------------------------------------------------


def test_acceptance_ingest_is_byte_idempotent(tmp_path: Path) -> None:
    """ROADMAP Phase 1 acceptance: running ingest twice changes nothing.

    Asserted at the byte level, which only holds because the snapshot
    carries no wall-clock data and sorts deterministically (DATA.md §5).
    A weaker "same values" assertion would let a timestamp or dict-ordering
    regression through.
    """
    first, _ = run_pipeline(SEASON, "fixture", AS_OF, tmp_path, verbose=False)
    snapshot_path = tmp_path / "snapshots" / str(SEASON) / f"{AS_OF.isoformat()}.json"
    first_bytes = snapshot_path.read_bytes()

    run_pipeline(SEASON, "fixture", AS_OF, tmp_path, verbose=False)
    assert snapshot_path.read_bytes() == first_bytes

    # latest.json must track the snapshot exactly, not drift from it.
    assert (tmp_path / "latest.json").read_bytes() == first_bytes
    assert first.qa_passed


def test_snapshot_contains_no_wallclock_fields(tmp_path: Path) -> None:
    """The determinism rule, enforced directly rather than implied.

    If someone later adds `generated_at` to the payload "for convenience",
    idempotency breaks in a way that's confusing to debug. Fail here, at the
    cause, with an explanation.
    """
    run_pipeline(SEASON, "fixture", AS_OF, tmp_path, verbose=False)
    payload = (tmp_path / "latest.json").read_text(encoding="utf-8")

    for forbidden in ("generated_at", "started_at", "finished_at", "run_id"):
        assert forbidden not in payload, (
            f"{forbidden!r} found in the snapshot. Wall-clock and run-specific "
            "data belongs in data/ingest_runs.jsonl, not the snapshot — see "
            "DATA.md §5. Its presence breaks byte-level idempotency."
        )


def test_run_log_records_every_run(tmp_path: Path) -> None:
    run_pipeline(SEASON, "fixture", AS_OF, tmp_path, verbose=False)
    run_pipeline(SEASON, "fixture", AS_OF, tmp_path, verbose=False)

    lines = (tmp_path / "ingest_runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        entry = json.loads(line)
        assert entry["status"] == "success"
        assert entry["rows_written"]["teams"] == 30
        # Timestamps must live here, precisely because they don't live in the
        # snapshot.
        assert entry["started_at"] and entry["finished_at"]


# --------------------------------------------------------------------------
# ACCEPTANCE 2 — a malformed payload is rejected with a clear error
# --------------------------------------------------------------------------


def test_acceptance_malformed_payload_is_rejected_not_written(
    normalized_state: PipelineState, tmp_path: Path
) -> None:
    """ROADMAP Phase 1 acceptance: rejected with a clear error, not written.

    Corrupts a single value in an otherwise-valid season and checks both
    halves of the promise: the run fails, *and* the error names the offending
    row well enough to act on.
    """
    corrupted = normalized_state.player_box.copy()
    corrupted.loc[corrupted.index[5], "points"] = -999.0

    with pytest.raises(BoundaryValidationError) as caught:
        validate.run(normalized_state.evolve(player_box=corrupted))

    message = str(caught.value)
    assert "player_box" in message
    assert "points" in message
    # An error that doesn't identify the row is not actionable.
    assert "game_id" in message
    assert "do not relax the model" in message

    # And nothing was published.
    assert not (tmp_path / "latest.json").exists()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("points", -5.0, "negative counting stat"),
        ("minutes", 500.0, "impossible minutes"),
        ("athlete_id", None, "unkeyable row"),
        ("home_away", "sideways", "not a known value"),
    ],
)
def test_player_row_rejects_bad_values(
    normalized_state: PipelineState, field: str, value: object, reason: str
) -> None:
    record = normalized_state.player_box.to_dict("records")[0]
    with pytest.raises(ValidationError):
        PlayerBoxRow.model_validate({**record, field: value})


def test_made_cannot_exceed_attempted(normalized_state: PipelineState) -> None:
    record = normalized_state.player_box.to_dict("records")[0]
    with pytest.raises(ValidationError, match="exceeds"):
        PlayerBoxRow.model_validate(
            {**record, "field_goals_made": 40.0, "field_goals_attempted": 2.0}
        )


def test_turnover_identity_is_enforced(normalized_state: PipelineState) -> None:
    """If the three turnover columns stop reconciling, the provider changed a
    column's meaning and the possessions formula needs re-deriving. Worth
    failing a run over."""
    record = normalized_state.team_box.to_dict("records")[0]
    with pytest.raises(ValidationError, match="total_turnovers"):
        TeamBoxRow.model_validate({**record, "total_turnovers": 999.0})


def test_missing_fixture_fails_clearly() -> None:
    with pytest.raises(ProviderError, match="missing fixture"):
        FixtureProvider("no-such-dir").get_box_scores(SEASON)


def test_unknown_provider_fails_clearly() -> None:
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("not-a-provider")


# --------------------------------------------------------------------------
# Rating math — reconciled against Basketball-Reference in Phase 0
# --------------------------------------------------------------------------


def test_league_offensive_and_defensive_rating_are_equal(
    rated_state: PipelineState,
) -> None:
    """The strongest single check that possessions are coherent.

    One team's points scored are another's points allowed, so league ORtg and
    league DRtg must be the same number. A one-sided error in the possession
    estimate breaks this immediately.
    """
    teams = rated_state.team_ratings
    league_off = 100 * teams["pts"].sum() / teams["poss"].sum()
    league_def = 100 * teams["opp_pts"].sum() / teams["opp_poss"].sum()
    assert league_off == pytest.approx(league_def, abs=0.01)


@pytest.mark.parametrize(
    "abbr,off_rtg,def_rtg",
    [
        # Basketball-Reference 2025-26 Team Ratings (unadjusted columns).
        # Tolerance 0.35 covers the small residual from our turnover repair,
        # which BRef does not apply. See MEMORY.md reconciliation log.
        ("OKC", 118.94, 107.89),
        ("BOS", 120.82, 112.67),
        ("DEN", 122.63, 117.46),
        ("WSH", 111.00, 122.84),
        ("BKN", 108.84, 119.11),
    ],
)
def test_team_ratings_match_basketball_reference(
    rated_state: PipelineState, abbr: str, off_rtg: float, def_rtg: float
) -> None:
    row = rated_state.team_ratings.set_index("abbr").loc[abbr]
    assert row["off_rtg"] == pytest.approx(off_rtg, abs=0.35)
    assert row["def_rtg"] == pytest.approx(def_rtg, abs=0.35)


def test_possessions_use_the_precise_two_sided_formula(
    normalized_state: PipelineState,
) -> None:
    """Guards against a well-meaning "simplification" back to
    `FGA + 0.44*FTA - OREB + TOV`.

    That formula is self-consistent, so the league-symmetry test above would
    still pass with it — but it reconciles against Basketball-Reference with a
    systematic multi-point offset on both ratings. This pins the league
    average to the value the precise formula produces.
    """
    teams = ratings.team_ratings(normalized_state.team_box)
    league_off = 100 * teams["pts"].sum() / teams["poss"].sum()
    assert league_off == pytest.approx(115.81, abs=0.1), (
        "league ORtg moved. If the possessions formula changed, re-run the "
        "Basketball-Reference reconciliation before updating this number."
    )


def test_games_played_uses_minutes_not_active_column(
    rated_state: PipelineState,
) -> None:
    """Gobert: 79 box-score rows, 76 games actually played, BRef says 76.

    The `active` column reads True on only 14 of those games, so using it
    would be badly wrong. This is the exact discrepancy Phase 0 left open.
    """
    players = rated_state.player_ratings
    gobert = players[players["name"] == "Rudy Gobert"]
    assert len(gobert) == 1, "Gobert should have exactly one stint"
    assert int(gobert.iloc[0]["gp"]) == 76


@pytest.mark.parametrize(
    "name,bref_games_played",
    [
        # Both San Antonio, both off by exactly +1 before the NBA Cup
        # Championship was excluded.
        ("Luke Kornet", 68),
        ("Victor Wembanyama", 64),
        # Control: a player on a team that never reached the Cup final, so
        # his count was already right and must stay right.
        ("Chet Holmgren", 69),
    ],
)
def test_games_played_matches_basketball_reference(
    rated_state: PipelineState, name: str, bref_games_played: int
) -> None:
    players = rated_state.player_ratings
    row = players[players["name"] == name]
    assert len(row) == 1
    assert int(row.iloc[0]["gp"]) == bref_games_played


def test_nba_cup_championship_is_excluded(rated_state: PipelineState) -> None:
    """Every team must show 82 regular-season games.

    ESPN ships the NBA Cup Championship tagged season_type == 2, which puts
    the two finalists at 83 and throws off gp and pace for ~35 players.
    Officially it does not count — and neither does Basketball-Reference
    count it — while every other Cup game (group play, quarters, semis) does.
    Excluded by its schedule `type_id`, not by a date heuristic.
    """
    teams = rated_state.team_ratings
    assert set(teams["gp"].unique()) == {82}, (
        "expected all 30 teams at exactly 82 games; got "
        f"{sorted(teams['gp'].unique())}"
    )


def test_cup_group_play_games_still_count(normalized_state: PipelineState) -> None:
    """Guard against over-correcting.

    Only the Championship is excluded. Cup group-play, quarterfinal and
    semifinal games are ordinary regular-season games — dropping them too
    would quietly delete ~66 games of real data.
    """
    schedule = normalized_state.raw_schedule
    kept_game_ids = set(normalized_state.team_box["game_id"])

    group_play = schedule[
        schedule["notes_headline"].fillna("").str.startswith("NBA Cup - ")
    ]
    assert not group_play.empty, "fixture no longer exercises this case"

    game_id_column = "id" if "id" in schedule.columns else "game_id"
    still_present = sum(
        1 for gid in group_play[game_id_column] if gid in kept_game_ids
    )
    assert still_present == len(group_play), (
        f"only {still_present} of {len(group_play)} NBA Cup group/knockout "
        "games survived normalize; those all count toward the regular season"
    )


# --------------------------------------------------------------------------
# Quirk handling
# --------------------------------------------------------------------------


def test_exhibition_teams_are_excluded(normalized_state: PipelineState) -> None:
    """All-Star squads carry season_type == 2 like real games, so only the
    team_id range separates them. Left in, they distort team ratings badly
    (pace ~33 against a real ~100)."""
    team_ids = set(normalized_state.team_box["team_id"].unique())
    assert team_ids <= set(range(1, 31))
    assert len(team_ids) == 30


def test_null_athlete_rows_are_dropped_and_counted(
    normalized_state: PipelineState,
) -> None:
    assert normalized_state.player_box["athlete_id"].notna().all()
    # int, not float — a float primary key invites subtle join bugs.
    assert normalized_state.player_box["athlete_id"].dtype == "int64"

    kinds = {note.kind: note.count for note in normalized_state.notes}
    assert kinds.get("dropped_null_athlete_id", 0) > 0


def test_underreported_turnovers_are_repaired_and_counted(
    normalized_state: PipelineState,
) -> None:
    team_box = normalized_state.team_box
    assert (team_box["possession_turnovers"] >= team_box["turnovers"]).all()
    assert (team_box["possession_turnovers"] >= team_box["total_turnovers"]).all()

    kinds = {note.kind: note.count for note in normalized_state.notes}
    assert kinds.get("repaired_underreported_total_turnovers", 0) == 4


def test_negative_team_turnovers_are_tolerated() -> None:
    """`team_turnovers` is a derived residual and legitimately goes negative.
    Rejecting it would kill a nightly run over 4 upstream rows."""
    raw = pd.read_parquet(FIXTURES / f"team_box_{SEASON}.parquet")
    negative = raw[raw["team_turnovers"] < 0]
    assert not negative.empty, "fixture no longer exercises this case"
    for record in negative.to_dict("records"):
        TeamBoxRow.model_validate(record)  # must not raise


# --------------------------------------------------------------------------
# Transactions / stints
# --------------------------------------------------------------------------


def test_every_rated_player_has_exactly_one_row_per_stint(
    rated_state: PipelineState,
) -> None:
    players = rated_state.player_ratings
    assert not players["stint_id"].duplicated().any()


def test_stint_ids_are_deterministic(normalized_state: PipelineState) -> None:
    """Stint ids feed the snapshot, so they must not depend on run order or
    anything incidental."""
    first = transactions.run(normalized_state).stints
    second = transactions.run(normalized_state).stints
    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True)
    )


def test_multi_stint_players_are_split(rated_state: PipelineState) -> None:
    """A traded player must produce separate stints, because stint
    possessions become `n` in Phase 2's shrinkage weight (DATA.md §6a)."""
    stints = rated_state.stints
    counts = stints.groupby("athlete_id").size()
    assert (counts > 1).any(), "expected at least one player to have moved teams"

    moved = counts[counts > 1].index[0]
    player_stints = stints[stints["athlete_id"] == moved].sort_values("stint_sequence")
    assert player_stints["team_id"].nunique() >= 2
    # First stint of a season is not an acquisition event we can label.
    assert player_stints.iloc[0]["acquisition_type"] == "season_start"
    assert player_stints.iloc[1]["acquisition_type"] == "unknown"


def test_qa_rejection_prevents_publish(rated_state: PipelineState, tmp_path: Path) -> None:
    """QA below threshold must write nothing — the last good snapshot keeps
    serving rather than being replaced by something wrong."""
    failed = rated_state.evolve(qa_passed=False, qa_confidence=0.1)
    with pytest.raises(publish.QaRejection, match="nothing written"):
        publish.run(failed, data_root=tmp_path)
    assert not (tmp_path / "latest.json").exists()
