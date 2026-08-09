"""
qa — integrity checks producing a 0-1 confidence score plus flags.

Mirrors xml-auditor's `qa_agent`: start at 1.0, subtract per finding, and
below a threshold reject the run so the previous snapshot keeps serving.
"Fail loudly to the maintainer, quietly to users" — a stale chart is
recoverable, a wrong one is not.

Scope note: these are *data integrity* checks (MODEL.md §6) — impossible
values, things not reconciling, populations changing size unexpectedly.
Statistical outlier detection (median/MAD, Mahalanobis over the ORtg/DRtg
pair) is a different concern and belongs to Phase 2, MODEL.md §5. A player
with an extreme-but-real rating is not a data-quality problem and must not
be flagged as one here.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..state import PipelineState

STAGE = "qa"

SEVERITY_PENALTY = {"error": 0.35, "warn": 0.15}
CONFIDENCE_THRESHOLD = 0.5

EXPECTED_TEAM_COUNT = 30
# 82 games each, plus the NBA Cup final which counts as a regular-season
# game for its two finalists only — so 30*82/2 games, +1. Observed in
# 2025-26: San Antonio and New York both played 83.
EXPECTED_TEAM_GAME_ROWS_MIN = 2400
EXPECTED_TEAM_GAME_ROWS_MAX = 2500

# Plausibility envelope for a season-level team rating. Deliberately wide:
# this catches a broken formula (a 40 or a 400), not an unusually good or
# bad team. The 2025-26 spread ran 108.7-122.6.
TEAM_RATING_FLOOR = 90.0
TEAM_RATING_CEILING = 140.0

# Team-vs-player points reconciliation, as a fraction of team points.
# Above the error line means a structural problem on our side (a broken
# join loses whole rosters, which is orders of magnitude worse than any
# provider gap). Between the two lines means the provider has holes worth
# knowing about but not worth refusing to ship over — the observed 2025-26
# case is the Bulls at 0.29%.
RECONCILE_ERROR_FRACTION = 0.02
RECONCILE_WARN_FRACTION = 0.0005


def _flag(check: str, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": check, "severity": severity, "detail": detail, **extra}


def run(state: PipelineState) -> PipelineState:
    teams = state.team_ratings
    players = state.player_ratings
    team_box = state.team_box
    if teams is None or players is None or team_box is None:
        raise ValueError("qa requires aggregated ratings; run aggregate first")

    flags: list[dict[str, Any]] = []

    # --- Population size --------------------------------------------------
    if len(teams) != EXPECTED_TEAM_COUNT:
        flags.append(
            _flag(
                "team_count",
                "error",
                f"expected {EXPECTED_TEAM_COUNT} teams, got {len(teams)}",
            )
        )

    if not (
        EXPECTED_TEAM_GAME_ROWS_MIN <= len(team_box) <= EXPECTED_TEAM_GAME_ROWS_MAX
    ):
        # Warn, not error: mid-season runs legitimately have fewer rows.
        # This is a "does the shape look like a season" check, and it only
        # becomes damning once the season is complete.
        flags.append(
            _flag(
                "team_game_row_count",
                "warn",
                f"{len(team_box)} team-game rows is outside the expected "
                f"{EXPECTED_TEAM_GAME_ROWS_MIN}-{EXPECTED_TEAM_GAME_ROWS_MAX} "
                "for a complete regular season (expected mid-season)",
            )
        )

    if players.empty:
        flags.append(_flag("player_count", "error", "no player rows produced"))

    # --- Impossible / implausible values ----------------------------------
    for column in ("off_rtg", "def_rtg"):
        out_of_range = teams[
            (teams[column] < TEAM_RATING_FLOOR) | (teams[column] > TEAM_RATING_CEILING)
        ]
        if not out_of_range.empty:
            flags.append(
                _flag(
                    f"team_{column}_envelope",
                    "error",
                    f"{len(out_of_range)} team(s) outside "
                    f"{TEAM_RATING_FLOOR}-{TEAM_RATING_CEILING}: "
                    + ", ".join(
                        f"{row['abbr']} {row[column]:.1f}"
                        for _, row in out_of_range.iterrows()
                    ),
                )
            )

    negative_poss = teams[teams["poss"] <= 0]
    if not negative_poss.empty:
        flags.append(
            _flag(
                "team_possessions_positive",
                "error",
                f"{len(negative_poss)} team(s) with non-positive possessions",
            )
        )

    impossible_minutes = players[players["mp"] > players["gp"] * 48 + 1e-6]
    if not impossible_minutes.empty:
        flags.append(
            _flag(
                "player_minutes_vs_games",
                "warn",
                f"{len(impossible_minutes)} player stint(s) with minutes above "
                "games x 48 (possible in overtime, worth watching)",
            )
        )

    # --- Reconciliation ---------------------------------------------------
    # League ORtg must equal league DRtg: one team's points scored are
    # another's points allowed, so the two aggregate to the same number by
    # construction. This is the single strongest check that the possessions
    # formula is coherent — it caught nothing but would have caught a
    # one-sided error immediately.
    league_off = 100 * teams["pts"].sum() / teams["poss"].sum()
    league_def = 100 * teams["opp_pts"].sum() / teams["opp_poss"].sum()
    if abs(league_off - league_def) > 0.05:
        flags.append(
            _flag(
                "league_rating_symmetry",
                "error",
                f"league ORtg {league_off:.3f} != league DRtg {league_def:.3f}; "
                "the possessions estimate is not self-consistent",
                league_off_rtg=round(league_off, 3),
                league_def_rtg=round(league_def, 3),
            )
        )

    # Team points vs the sum of their players' points, judged by *relative*
    # size. An absolute threshold cannot tell the two failure modes apart,
    # and they need opposite responses:
    #
    #   * A broken join or dropped roster produces an enormous relative gap
    #     and must fail the run — that is our bug.
    #   * ESPN's own box scores have small gaps. In 2025-26 the Bulls are
    #     short 28 points across 7 games (0.29% of their season total):
    #     scoring present in the team line but absent from the player lines.
    #     Failing the nightly run forever over an upstream gap we cannot fix
    #     would mean never shipping.
    #
    # So: warn when it is visible, error only when it is big enough to mean
    # something is structurally wrong on our side.
    player_points = players.groupby("team_id")["pts"].sum()
    team_points = teams.set_index("team_id")["pts"]
    absolute_gap = (player_points - team_points).abs()
    relative_gap = (absolute_gap / team_points.replace(0, pd.NA)).astype(float)

    for severity, floor in (("error", RECONCILE_ERROR_FRACTION),
                            ("warn", RECONCILE_WARN_FRACTION)):
        offenders = relative_gap[relative_gap > floor]
        if severity == "error":
            reportable = offenders
        else:
            # Don't double-report a team already flagged as an error.
            reportable = offenders[offenders <= RECONCILE_ERROR_FRACTION]
        if not reportable.empty:
            worst_team = reportable.idxmax()
            flags.append(
                _flag(
                    "team_player_points_reconcile",
                    severity,
                    f"{len(reportable)} team(s) where summed player points "
                    f"differ from the team total by more than {floor:.2%}; "
                    f"worst is team_id={worst_team} off by "
                    f"{absolute_gap[worst_team]:.0f} points "
                    f"({reportable.max():.2%})",
                    worst_team_id=int(worst_team),
                    worst_absolute_points=float(absolute_gap[worst_team]),
                    worst_fraction=round(float(reportable.max()), 5),
                )
            )

    # --- Duplicates -------------------------------------------------------
    duplicate_stints = players["stint_id"].duplicated().sum()
    if duplicate_stints:
        flags.append(
            _flag(
                "duplicate_stint_rows",
                "error",
                f"{duplicate_stints} duplicate stint_id(s) in player ratings",
            )
        )

    # --- Carry normalize/transactions interventions into the flag list ----
    # A repair is not a failure, but it should be visible rather than buried
    # in a log nobody reads.
    for note in state.notes:
        if note.kind == "repaired_underreported_total_turnovers":
            flags.append(
                _flag(
                    "provider_data_repaired",
                    "warn",
                    f"{note.count} team-game row(s) had under-reported "
                    "total_turnovers; repaired via max(total, player-attributable)",
                )
            )
        elif note.kind == "schedule_metadata_unavailable":
            flags.append(
                _flag(
                    "schedule_metadata_unavailable",
                    "warn",
                    "schedule type_id unavailable, so the NBA Cup Championship "
                    "could not be excluded; gp and pace may be one game high "
                    "for the two Cup finalists",
                )
            )

    confidence = 1.0
    for flag in flags:
        confidence -= SEVERITY_PENALTY.get(flag["severity"], 0.0)
    confidence = max(0.0, round(confidence, 4))

    passed = confidence >= CONFIDENCE_THRESHOLD and not any(
        flag["severity"] == "error" for flag in flags
    )

    return state.evolve(
        qa_confidence=confidence,
        qa_flags=tuple(flags),
        qa_passed=passed,
    )
