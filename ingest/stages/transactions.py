"""
transactions — reconstruct player team-stints and tag how each began.

Full design in DATA.md §6a. Its own stage rather than an ad-hoc rule
inside aggregation, because it changes what "current sample" *means* for a
traded player, and that ripples into the model.

A **stint** is a contiguous span of games with one team. The output matters
for two reasons:

1. **It sets `n` for shrinkage.** MODEL.md §2's reliability weight
   `w = n/(n+k)` uses the *current stint's* possessions, not full-season
   combined. A trade is new context — new role, new teammates, new system —
   so the honest sample is the one since the move, and a mid-season arrival
   should read as less certain. Using combined possessions would overstate
   confidence in a number measured under different conditions.
2. **It gives the UI something true to say.** Surfacing "traded on 5 Feb"
   explains why a rotation player looks uncertain, instead of leaving a fan
   to assume the chart is broken.

**Scope limit, stated plainly:** this infers boundaries from the game log
alone, so it knows a player *changed teams* but not *why*. Every boundary
is therefore tagged `boundary_source="game_log_inference"` and
`acquisition_type="unknown"` unless it is the player's first stint of the
season (`season_start`). Distinguishing trade from signing from waiver
claim from G-League call-up needs transaction data — hoopR ships
`nba/rosters` and `nba/game_rosters`, which are the obvious next input.
Deliberately deferred rather than guessed: a plausible-looking wrong label
("Traded to Phoenix") is worse than an honest absent one, because a fan
would believe it.
"""

from __future__ import annotations

import pandas as pd

from ..state import PipelineState

STAGE = "transactions"


def _stint_id(athlete_id: int, team_id: int, sequence: int) -> str:
    """Stable, readable stint key.

    Derived purely from identity and ordering — no timestamps, no hashes of
    run-specific data — so the same input always produces the same id. The
    snapshot has to be byte-reproducible (DATA.md §5), which rules out
    anything incidental in a key.
    """
    return f"{athlete_id}-{team_id}-{sequence}"


def run(state: PipelineState) -> PipelineState:
    player_box = state.player_box
    if player_box is None:
        raise ValueError("transactions requires normalized box scores")

    played = player_box[player_box["played"]].copy()
    played = played.sort_values(["athlete_id", "game_date", "game_id"])

    # A new stint starts wherever a player's team differs from his team in
    # the previous chronological game. Sorting by athlete then date makes
    # this a single shift comparison.
    previous_team = played.groupby("athlete_id")["team_id"].shift()
    is_new_stint = previous_team.isna() | (played["team_id"] != previous_team)
    played["stint_sequence"] = is_new_stint.groupby(played["athlete_id"]).cumsum()

    played["stint_id"] = [
        _stint_id(athlete, team, int(sequence))
        for athlete, team, sequence in zip(
            played["athlete_id"], played["team_id"], played["stint_sequence"]
        )
    ]

    stints = (
        played.groupby(["athlete_id", "team_id", "stint_sequence", "stint_id"])
        .agg(
            start_date=("game_date", "min"),
            end_date=("game_date", "max"),
            gp=("game_id", "nunique"),
        )
        .reset_index()
        .sort_values(["athlete_id", "stint_sequence"])
    )

    stints["boundary_source"] = stints["stint_sequence"].map(
        lambda sequence: "season_start" if sequence == 1 else "game_log_inference"
    )
    stints["acquisition_type"] = stints["stint_sequence"].map(
        lambda sequence: "season_start" if sequence == 1 else "unknown"
    )

    multi_stint_players = int((stints.groupby("athlete_id").size() > 1).sum())

    # Carry stint_id back onto the game rows so the aggregate stage can
    # group by it.
    player_box = player_box.merge(
        played[["game_id", "athlete_id", "stint_id"]],
        on=["game_id", "athlete_id"],
        how="left",
    )

    new_state = state.evolve(player_box=player_box, stints=stints)
    new_state = new_state.with_note(
        STAGE, "stints_resolved", len(stints),
        f"{multi_stint_players} players had more than one stint this season",
    )
    return new_state.with_note(
        STAGE, "acquisition_type_unknown",
        int((stints["acquisition_type"] == "unknown").sum()),
        "mid-season stint boundaries inferred from the game log; labelling "
        "trade vs signing vs call-up needs hoopR's nba/rosters data (DATA.md §6a)",
    )
