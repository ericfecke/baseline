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

**Scope limit, stated plainly — this was investigated, not assumed.**
hoopR ships **no transactions feed**. Its `nba/rosters` file is a current
snapshot (537 rows, every status "Active", no dates and no history), and
`game_rosters.reason` holds injury and DNP text ("COACH'S DECISION",
"ILLNESS"), not player movement. So the *mechanism* of a mid-season move —
trade vs. waiver claim vs. buyout-and-sign vs. G-League call-up — is not
derivable from any free source currently in play.

What the available data does support, honestly:

* `nba/player_core` carries `draft_year`, verified correct against the
  known 2025 draft class, which identifies rookies.
* The game log itself distinguishes a player who *moved between NBA teams*
  (he has an earlier stint this season) from one *appearing for the first
  time* (he does not).

So we emit `rookie_debut`, `team_change` and `mid_season_debut` rather than
a blanket `unknown`. Each is a claim the data actually supports. We do not
emit "trade", because labelling a waiver claim as a trade would be a
confident lie, and a fan reading it would believe it. If a transactions
source ever appears, the finer labels slot in behind this same interface.
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


def _rookie_athlete_ids(player_core: pd.DataFrame | None, season: int) -> set[int]:
    """Athletes drafted for this season.

    The draft happens in the June *before* the season starts, so the
    2025-26 season (our `season == 2026`) has the 2025 draft class as its
    rookies — hence `season - 1`.

    Uses `draft_year` rather than `experience_years`, which is unreliable
    here: hoopR reports `experience_years == 1` for first-year players like
    Cooper Flagg, while 22 other players show 0, so it doesn't cleanly
    identify a rookie.
    """
    if player_core is None or "draft_year" not in player_core.columns:
        return set()

    drafted_this_cycle = player_core[player_core["draft_year"] == season - 1]
    return set(drafted_this_cycle["athlete_id"].dropna().astype("int64"))


def _classify(
    stint_sequence: int, athlete_id: int, rookie_ids: set[int]
) -> tuple[str, str]:
    """Return (acquisition_type, boundary_source) for one stint.

    Deliberately three outcomes, not four. A "signed mid-season" label was
    considered and rejected: separating it from "on the opening roster"
    needs a cutoff on how late a player's first game came, and the 2025-26
    distribution has no natural break to put one at — 332 players debut on
    their team's opening night and the rest decay smoothly out to 173 days,
    with no gap. Any threshold would be invented, and inventing one to
    generate a label is exactly the kind of confident-but-unfounded claim
    this stage avoids.

    The UI loses nothing: every stint publishes `start_date`, so "first
    appeared 12 February" is available as fact rather than as a category we
    made up.
    """
    if stint_sequence > 1:
        # He was on another NBA team earlier this season. We know he moved;
        # we don't know by what mechanism.
        return "team_change", "game_log_inference"

    if athlete_id in rookie_ids:
        return "rookie_debut", "draft_data"

    return "season_start", "season_start"


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

    rookie_ids = _rookie_athlete_ids(state.raw_player_core, state.season)
    classified = [
        _classify(int(sequence), int(athlete), rookie_ids)
        for sequence, athlete in zip(stints["stint_sequence"], stints["athlete_id"])
    ]
    stints["acquisition_type"] = [kind for kind, _ in classified]
    stints["boundary_source"] = [source for _, source in classified]

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
    for kind, count in stints["acquisition_type"].value_counts().items():
        new_state = new_state.with_note(
            STAGE, f"acquisition_{kind}", int(count), ""
        )

    if not rookie_ids:
        new_state = new_state.with_note(
            STAGE, "draft_data_unavailable", 1,
            "no player_core draft_year available, so rookies could not be "
            "distinguished from other first-season stints",
        )

    return new_state.with_note(
        STAGE, "acquisition_mechanism_unavailable",
        int((stints["acquisition_type"] == "team_change").sum()),
        "mid-season moves are labelled team_change only; trade vs waiver vs "
        "buyout is not derivable — hoopR ships no transactions feed "
        "(DATA.md §6a)",
    )
