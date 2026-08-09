"""
aggregate — season-level team and player ratings.

Thin by design: the arithmetic lives in `ingest/ratings.py`, which is the
module that was reconciled against Basketball-Reference. This stage's only
job is deciding *what gets grouped*, which is where the stint decision from
DATA.md §6a takes effect.
"""

from __future__ import annotations

from .. import ratings
from ..state import PipelineState

STAGE = "aggregate"


def run(state: PipelineState) -> PipelineState:
    if state.player_box is None or state.team_box is None:
        raise ValueError("aggregate requires normalized box scores")

    team_rows = ratings.team_ratings(state.team_box)

    # Group by stint, not by (player, team). For the ~99% of players who
    # never move this is identical; for a player traded and later reacquired
    # by the same team it correctly keeps the two spells apart, which
    # grouping on (player, team) would silently merge.
    player_rows = ratings.player_ratings(
        state.player_box,
        state.team_box,
        team_rows,
        group_keys=["athlete_id", "team_id", "stint_id"],
    )

    return (
        state.evolve(team_ratings=team_rows, player_ratings=player_rows)
        .with_note(STAGE, "teams_rated", len(team_rows))
        .with_note(STAGE, "player_stints_rated", len(player_rows))
    )
