"""
normalize — canonical shape, real teams only, characterised quirks handled.

Every intervention here is *counted* into `state.notes` so QA can surface
it. The governing rule (MEMORY.md): quirks we have characterised get
filtered or repaired and counted; anything uncharacterised fails loudly in
the validate stage instead.

This stage deliberately does **no** statistical cleaning. Small-sample
outliers stay in — a bench player with 40 possessions and a 160 offensive
rating is real data, and the answer to him is shrinkage in Phase 2, not a
filter here (DATA.md §6, MODEL.md §1). Ingest's job is to capture
possessions accurately so the model can weight by them.
"""

from __future__ import annotations

import pandas as pd

from ..models import (
    NON_COUNTING_GAME_TYPE_IDS,
    REAL_TEAM_IDS,
    SEASON_TYPE_REGULAR,
)
from ..state import PipelineState

STAGE = "normalize"


def _non_counting_game_ids(schedule: pd.DataFrame | None) -> set[int]:
    """Games tagged regular-season that don't count toward regular-season stats.

    Read from the schedule's own `type_id`, not inferred from dates or game
    counts. The NBA Cup Championship is the case that matters: ESPN ships it
    as season_type 2, but officially it doesn't count, which is why
    Basketball-Reference has San Antonio and New York at 82 games where the
    raw feed says 83. Every other Cup game does count and is typed STD.

    Returns an empty set if the schedule is unavailable or lacks the column,
    so a provider without schedule metadata degrades to including the game
    rather than crashing — the caller records the difference either way.
    """
    if schedule is None or "type_id" not in schedule.columns:
        return set()

    game_id_column = "id" if "id" in schedule.columns else "game_id"
    if game_id_column not in schedule.columns:
        return set()

    excluded = schedule[schedule["type_id"].isin(NON_COUNTING_GAME_TYPE_IDS)]
    return set(excluded[game_id_column].astype("int64"))


def _repair_possession_turnovers(team_box: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Add `possession_turnovers`, repairing under-reported totals.

    `total_turnovers` is occasionally reported *below* the
    player-attributable `turnovers`, which is impossible — a total cannot
    be less than one of its parts. Worst observed case is game 401810469,
    where both teams show 0 total turnovers while their players recorded 16
    and 12 between them.

    Taking the max repairs the floor without inventing data: we never claim
    more turnovers than one of the two sources reports. Mirrors
    `TeamBoxRow.possession_turnovers`, which is the canonical definition.
    """
    tb = team_box.copy()
    needs_repair = tb["total_turnovers"] < tb["turnovers"]
    tb["possession_turnovers"] = tb[["total_turnovers", "turnovers"]].max(axis=1)
    return tb, int(needs_repair.sum())


def run(state: PipelineState) -> PipelineState:
    player_box = state.raw_player_box
    team_box = state.raw_team_box
    if player_box is None or team_box is None:
        raise ValueError("normalize requires raw box scores; run fetch first")

    notes: list[tuple[str, int, str]] = []

    # --- Season type ------------------------------------------------------
    # v1 is regular season only. Postseason (3) and play-in (5) are real
    # basketball but a different population — mixing them would silently
    # blend contexts in a season-to-date rating.
    before = len(player_box)
    player_box = player_box[player_box["season_type"] == SEASON_TYPE_REGULAR]
    team_box = team_box[team_box["season_type"] == SEASON_TYPE_REGULAR]
    notes.append(
        ("dropped_non_regular_season", before - len(player_box), "season_type != 2")
    )

    # --- Games that don't count toward regular-season stats ---------------
    # Identified from the schedule's own type_id rather than guessed. Without
    # this, San Antonio and New York show 83 games against Basketball-
    # Reference's 82, and every one of their players' gp and pace is off by a
    # game.
    excluded_game_ids = _non_counting_game_ids(state.raw_schedule)
    if excluded_game_ids:
        before = len(player_box)
        player_box = player_box[~player_box["game_id"].isin(excluded_game_ids)]
        team_box = team_box[~team_box["game_id"].isin(excluded_game_ids)]
        notes.append(
            (
                "dropped_non_counting_games",
                before - len(player_box),
                f"{len(excluded_game_ids)} game(s) typed ALLSTAR/CC — "
                "NBA Cup Championship and All-Star exhibitions do not count "
                "toward regular-season totals",
            )
        )
    elif state.raw_schedule is None or "type_id" not in getattr(
        state.raw_schedule, "columns", []
    ):
        # Degrading quietly here would leave gp and pace wrong by a game for
        # every player on the two Cup finalists, with nothing to show why.
        # This exact silent path hid a bug in our own test fixture, so it
        # gets recorded and surfaced.
        notes.append(
            (
                "schedule_metadata_unavailable",
                1,
                "no schedule type_id available, so the NBA Cup Championship "
                "could not be excluded; gp and pace may be one game high for "
                "the two Cup finalists",
            )
        )

    # --- Exhibition teams -------------------------------------------------
    # All-Star Weekend squads (Team Stars / Team Stripes / World) carry
    # season_type == 2, identical to real games, so season_type alone does
    # NOT exclude them. They show up as extra "teams" with 2-3 games and a
    # pace around 33 versus a real ~100, which badly distorts team ratings.
    # Real franchises are exactly ESPN team_id 1-30. See MEMORY.md.
    before = len(player_box)
    player_box = player_box[player_box["team_id"].isin(REAL_TEAM_IDS)]
    team_box = team_box[
        team_box["team_id"].isin(REAL_TEAM_IDS)
        & team_box["opponent_team_id"].isin(REAL_TEAM_IDS)
    ]
    notes.append(
        (
            "dropped_exhibition_teams",
            before - len(player_box),
            "team_id outside the 30 real franchises (All-Star / Rising Stars)",
        )
    )

    # --- Unattributable rows ---------------------------------------------
    # A handful of rows have a null athlete_id. Provider IDs are the only
    # join key we use (DATA.md §6), so a row we cannot key is unusable.
    before = len(player_box)
    player_box = player_box[player_box["athlete_id"].notna()]
    notes.append(
        ("dropped_null_athlete_id", before - len(player_box), "cannot key the row")
    )

    # athlete_id arrives as float64 purely because the column had nulls.
    # Now that they are gone, make it a real integer — a float primary key
    # is a bug waiting to happen.
    player_box = player_box.copy()
    player_box["athlete_id"] = player_box["athlete_id"].astype("int64")

    # --- Participation ----------------------------------------------------
    # `minutes is not None` is the verified games-played signal. NOT the
    # `active` column, which tracks roster status: Gobert reads active=True
    # on only 14 of the 76 games he actually played. See MEMORY.md.
    player_box["played"] = player_box["minutes"].notna()

    # --- Turnover repair --------------------------------------------------
    team_box, repaired = _repair_possession_turnovers(team_box)
    notes.append(
        (
            "repaired_underreported_total_turnovers",
            repaired,
            "used max(total_turnovers, turnovers); affects the possessions estimate",
        )
    )

    # --- Colours ----------------------------------------------------------
    # Provider gives bare hex without the '#'. PRD §3 uses team colours as
    # the dot palette, so normalise the format once here rather than in the
    # browser.
    for column in ("team_color", "team_alternate_color"):
        team_box[column] = team_box[column].map(
            lambda value: f"#{value}" if isinstance(value, str) and value else None
        )

    new_state = state.evolve(player_box=player_box, team_box=team_box)
    for kind, count, detail in notes:
        new_state = new_state.with_note(STAGE, kind, count, detail)
    return new_state
