"""
Rating derivation — possessions, team ORtg/DRtg, Dean Oliver player ratings.

Validated in Phase 0 against Basketball-Reference: team ratings agree to
~0.2 points across all 30 teams, player ratings to ~0.5 across the six
spot-checked players. Full reconciliation table in MEMORY.md. **Do not
"tidy" the formulas below without re-running that reconciliation** — the
constants (0.4, 1.07, 0.44) and the exact term grouping are what make the
numbers match a published source.

Deriving ratings ourselves rather than buying them is the deliberate
strategy from DATA.md §2b: box scores are commodity data, ratings are the
scarce paywalled part, so any provider with a box score works.

Contract: these functions expect *normalized* frames from
`stages/normalize.py` — real teams only, one season type, `athlete_id`
as int, `possession_turnovers` present, `played` present. They do no
filtering of their own.

On vectorisation: the pipeline validates every row through Pydantic
(`stages/validate.py`) and then does the arithmetic in pandas. Round-
tripping 34k validated objects back into a frame would cost real time and
buy nothing — validation's job is to assert the data is sound, not to be
the calculation substrate.
"""

from __future__ import annotations

import pandas as pd

# Guards division where a denominator can legitimately be zero (a player
# with no field goal attempts, a team with no offensive rebounds). Small
# enough not to perturb any real quotient.
EPS = 1e-9

# Free-throw-to-possession coefficient. 0.44 in the simple formula, 0.4 in
# Oliver's possession estimate — they are different constants from
# different derivations, and both appear below intentionally.
FT_POSSESSION_COEFFICIENT = 0.4

# Weighting on missed field goals in the possession estimate, from
# Basketball on Paper.
MISSED_FG_COEFFICIENT = 1.07


def add_game_possessions(team_box: pd.DataFrame) -> pd.DataFrame:
    """Per game-team possession estimate, precise two-sided version.

    Averages both teams' independent estimates for the game and weights
    the missed-field-goal term by offensive rebound rate.

    This replaced the simpler `FGA + 0.44*FTA - OREB + TOV` during Phase 0.
    That formula is internally consistent — league ORtg equals league DRtg
    exactly either way, since one team's points are another's points
    allowed — but it reconciled against Basketball-Reference with a
    systematic 2.5-3.5 point offset on *both* ORtg and DRtg. Net rating
    looked fine, which is exactly why the bug hid: the offset cancels in
    the subtraction. Comparing absolute ratings against a published source
    is what caught it. See DATA.md §2b and MEMORY.md.
    """
    tb = team_box.copy()

    opponent = tb[
        [
            "game_id",
            "team_id",
            "field_goals_made",
            "field_goals_attempted",
            "free_throws_attempted",
            "offensive_rebounds",
            "defensive_rebounds",
            "possession_turnovers",
        ]
    ].rename(
        columns={
            "team_id": "opponent_team_id",
            "field_goals_made": "opp_fg",
            "field_goals_attempted": "opp_fga",
            "free_throws_attempted": "opp_fta",
            "offensive_rebounds": "opp_orb",
            "defensive_rebounds": "opp_drb",
            "possession_turnovers": "opp_tov",
        }
    )
    tb = tb.merge(opponent, on=["game_id", "opponent_team_id"], how="inner")

    own_estimate = (
        tb["field_goals_attempted"]
        + FT_POSSESSION_COEFFICIENT * tb["free_throws_attempted"]
        - MISSED_FG_COEFFICIENT
        * (tb["offensive_rebounds"] / (tb["offensive_rebounds"] + tb["opp_drb"] + EPS))
        * (tb["field_goals_attempted"] - tb["field_goals_made"])
        + tb["possession_turnovers"]
    )
    opponent_estimate = (
        tb["opp_fga"]
        + FT_POSSESSION_COEFFICIENT * tb["opp_fta"]
        - MISSED_FG_COEFFICIENT
        * (tb["opp_orb"] / (tb["opp_orb"] + tb["defensive_rebounds"] + EPS))
        * (tb["opp_fga"] - tb["opp_fg"])
        + tb["opp_tov"]
    )

    tb["poss"] = 0.5 * (own_estimate + opponent_estimate)
    return tb


def team_ratings(team_box: pd.DataFrame) -> pd.DataFrame:
    """Season-level team ORtg / DRtg / NetRtg / pace."""
    tb = add_game_possessions(team_box)

    agg = (
        tb.groupby("team_id")
        .agg(
            name=("team_display_name", "first"),
            abbr=("team_abbreviation", "first"),
            primary_color=("team_color", "first"),
            secondary_color=("team_alternate_color", "first"),
            gp=("game_id", "nunique"),
            pts=("team_score", "sum"),
            poss=("poss", "sum"),
            opp_pts=("opponent_team_score", "sum"),
        )
        .reset_index()
    )

    # Possessions *faced*: the opposing team's own possession count in each
    # game, summed. Not identical to `poss` — pace differs slightly between
    # the two sides of a game once the ORB weighting is applied.
    faced = tb[["game_id", "team_id", "poss"]].rename(
        columns={"team_id": "opponent_team_id", "poss": "opp_poss"}
    )
    faced = tb[["game_id", "team_id", "opponent_team_id"]].merge(
        faced, on=["game_id", "opponent_team_id"], how="inner"
    )
    agg = agg.merge(
        faced.groupby("team_id")["opp_poss"].sum().reset_index(), on="team_id"
    )

    agg["off_rtg"] = 100 * agg["pts"] / (agg["poss"] + EPS)
    agg["def_rtg"] = 100 * agg["opp_pts"] / (agg["opp_poss"] + EPS)
    agg["net_rtg"] = agg["off_rtg"] - agg["def_rtg"]
    agg["pace"] = agg["poss"] / (agg["gp"] + EPS)
    return agg


def _team_totals(player_box: pd.DataFrame, team_box: pd.DataFrame) -> pd.DataFrame:
    """Season team totals, plus what opponents did against them.

    Oliver's individual formulas need both: a player's offensive rating
    depends on his team's context, and his defensive rating depends on what
    opponents managed while he was on the floor.
    """
    totals = (
        team_box.groupby("team_id")
        .agg(
            team_fg=("field_goals_made", "sum"),
            team_fga=("field_goals_attempted", "sum"),
            team_3p=("three_point_field_goals_made", "sum"),
            team_ft=("free_throws_made", "sum"),
            team_fta=("free_throws_attempted", "sum"),
            team_orb=("offensive_rebounds", "sum"),
            team_drb=("defensive_rebounds", "sum"),
            team_ast=("assists", "sum"),
            team_tov=("possession_turnovers", "sum"),
            team_pf=("fouls", "sum"),
            team_pts=("team_score", "sum"),
            team_blk=("blocks", "sum"),
            team_stl=("steals", "sum"),
        )
        .reset_index()
    )

    # Team minutes = summed player minutes (5 x game minutes, including OT).
    team_minutes = (
        player_box.groupby("team_id")["minutes"]
        .sum()
        .reset_index()
        .rename(columns={"minutes": "team_mp"})
    )
    totals = totals.merge(team_minutes, on="team_id")

    own = team_box[
        [
            "game_id",
            "team_id",
            "field_goals_made",
            "field_goals_attempted",
            "free_throws_made",
            "free_throws_attempted",
            "offensive_rebounds",
            "defensive_rebounds",
            "possession_turnovers",
            "team_score",
        ]
    ].rename(columns={"team_id": "opponent_team_id"})
    joined = team_box[["game_id", "team_id", "opponent_team_id"]].merge(
        own, on=["game_id", "opponent_team_id"], how="inner"
    )
    opponent = (
        joined.groupby("team_id")
        .agg(
            opp_fg=("field_goals_made", "sum"),
            opp_fga=("field_goals_attempted", "sum"),
            opp_ft=("free_throws_made", "sum"),
            opp_fta=("free_throws_attempted", "sum"),
            opp_orb=("offensive_rebounds", "sum"),
            opp_drb=("defensive_rebounds", "sum"),
            opp_tov=("possession_turnovers", "sum"),
            opp_pts=("team_score", "sum"),
        )
        .reset_index()
    )

    return totals.merge(opponent, on="team_id")


def player_ratings(
    player_box: pd.DataFrame,
    team_box: pd.DataFrame,
    team_rating_rows: pd.DataFrame,
    group_keys: list[str] | None = None,
) -> pd.DataFrame:
    """Dean Oliver individual offensive and defensive ratings.

    Computed on season-aggregated totals, which is how Basketball-Reference
    derives its published table — *not* by averaging per-game ratings,
    which gives materially different (and wrong) answers.

    `group_keys` defaults to `["athlete_id", "team_id"]`, which naturally
    separates a traded player's stints. The transactions stage passes
    `["athlete_id", "team_id", "stint_id"]` so ratings are computed per
    stint, making a stint's possessions the correct `n` for Phase 2's
    shrinkage weight (DATA.md §6a).
    """
    keys = group_keys or ["athlete_id", "team_id"]

    played = player_box[player_box["played"]]

    aggregated = (
        played.groupby(keys)
        .agg(
            name=("athlete_display_name", "first"),
            position=("athlete_position_abbreviation", "first"),
            jersey=("athlete_jersey", "first"),
            # gp counts games with an actual box line. Verified against
            # Basketball-Reference: Gobert 76, not the 79 rows present.
            # Never use the `active` column here — it tracks roster status,
            # not participation. See MEMORY.md.
            gp=("game_id", "nunique"),
            mp=("minutes", "sum"),
            fg=("field_goals_made", "sum"),
            fga=("field_goals_attempted", "sum"),
            tp=("three_point_field_goals_made", "sum"),
            ft=("free_throws_made", "sum"),
            fta=("free_throws_attempted", "sum"),
            orb=("offensive_rebounds", "sum"),
            drb=("defensive_rebounds", "sum"),
            ast=("assists", "sum"),
            stl=("steals", "sum"),
            blk=("blocks", "sum"),
            # Individual turnovers, NOT the team total — team-charged
            # turnovers aren't attributable to a player's box line.
            tov=("turnovers", "sum"),
            pf=("fouls", "sum"),
            pts=("points", "sum"),
        )
        .reset_index()
    )

    totals = _team_totals(player_box, team_box)
    totals = totals.merge(
        team_rating_rows[["team_id", "poss"]].rename(columns={"poss": "team_poss"}),
        on="team_id",
    )
    p = aggregated.merge(totals, on="team_id", how="left")

    # --- Offensive rating -------------------------------------------------
    # qAST: share of a player's made field goals that were assisted, needed
    # to split credit between scorer and passer.
    q_assist = (
        (p["mp"] / (p["team_mp"] / 5 + EPS))
        * (1.14 * ((p["team_ast"] - p["ast"]) / (p["team_fg"] + EPS)))
    ) + (
        (
            ((p["team_ast"] / (p["team_mp"] + EPS)) * p["mp"] * 5 - p["ast"])
            / ((p["team_fg"] / (p["team_mp"] + EPS)) * p["mp"] * 5 - p["fg"] + EPS)
        )
        * (1 - (p["mp"] / (p["team_mp"] / 5 + EPS)))
    )
    q_assist = q_assist.clip(lower=0, upper=1.3).fillna(0)

    fg_part = p["fg"] * (
        1 - 0.5 * ((p["pts"] - p["ft"]) / (2 * p["fga"] + EPS)) * q_assist
    )
    assist_part = (
        0.5
        * (
            ((p["team_pts"] - p["team_ft"]) - (p["pts"] - p["ft"]))
            / (2 * (p["team_fga"] - p["fga"]) + EPS)
        )
        * p["ast"]
    )
    ft_part = (1 - (1 - (p["ft"] / (p["fta"] + EPS))) ** 2) * 0.4 * p["fta"]

    team_scoring_poss = (
        p["team_fg"]
        + (1 - (1 - (p["team_ft"] / (p["team_fta"] + EPS))) ** 2) * p["team_fta"] * 0.4
    )
    team_orb_pct = p["team_orb"] / (p["team_orb"] + p["opp_drb"] + EPS)
    team_play_pct = team_scoring_poss / (
        p["team_fga"] + p["team_fta"] * 0.4 + p["team_tov"] + EPS
    )
    team_orb_weight = ((1 - team_orb_pct) * team_play_pct) / (
        (1 - team_orb_pct) * team_play_pct + team_orb_pct * (1 - team_play_pct) + EPS
    )

    orb_part = p["orb"] * team_orb_weight * team_play_pct

    scoring_poss = (fg_part + assist_part + ft_part) * (
        1
        - (p["team_orb"] / (team_scoring_poss + EPS))
        * team_orb_weight
        * team_play_pct
    ) + orb_part

    missed_fg_poss = (p["fga"] - p["fg"]) * (1 - MISSED_FG_COEFFICIENT * team_orb_pct)
    missed_ft_poss = (1 - (p["ft"] / (p["fta"] + EPS))) ** 2 * 0.4 * p["fta"]
    total_poss = scoring_poss + missed_fg_poss + missed_ft_poss + p["tov"]

    points_fg_part = (
        2
        * (p["fg"] + 0.5 * p["tp"])
        * (1 - 0.5 * ((p["pts"] - p["ft"]) / (2 * p["fga"] + EPS)) * q_assist)
    )
    points_assist_part = (
        2
        * (
            (p["team_fg"] - p["fg"] + 0.5 * (p["team_3p"] - p["tp"]))
            / (p["team_fg"] - p["fg"] + EPS)
        )
        * 0.5
        * (
            ((p["team_pts"] - p["team_ft"]) - (p["pts"] - p["ft"]))
            / (2 * (p["team_fga"] - p["fga"]) + EPS)
        )
        * p["ast"]
    )
    points_orb_part = (
        p["orb"]
        * team_orb_weight
        * team_play_pct
        * (p["team_pts"] / (team_scoring_poss + EPS))
    )

    points_produced = (points_fg_part + points_assist_part + p["ft"]) * (
        1
        - (p["team_orb"] / (team_scoring_poss + EPS))
        * team_orb_weight
        * team_play_pct
    ) + points_orb_part

    p["off_rtg"] = 100 * (points_produced / (total_poss + EPS))
    p["poss"] = total_poss

    # --- Defensive rating -------------------------------------------------
    opp_orb_pct = p["opp_orb"] / (p["opp_orb"] + p["team_drb"] + EPS)
    opp_fg_pct = p["opp_fg"] / (p["opp_fga"] + EPS)
    missed_fg_weight = (opp_fg_pct * (1 - opp_orb_pct)) / (
        opp_fg_pct * (1 - opp_orb_pct) + (1 - opp_fg_pct) * opp_orb_pct + EPS
    )

    stops_individual = (
        p["stl"]
        + p["blk"] * missed_fg_weight * (1 - MISSED_FG_COEFFICIENT * opp_orb_pct)
        + p["drb"] * (1 - missed_fg_weight)
    )
    stops_team_share = (
        (
            ((p["opp_fga"] - p["opp_fg"] - p["team_blk"]) / (p["team_mp"] + EPS))
            * missed_fg_weight
            * (1 - MISSED_FG_COEFFICIENT * opp_orb_pct)
            + ((p["opp_tov"] - p["team_stl"]) / (p["team_mp"] + EPS))
        )
        * p["mp"]
        + (p["pf"] / (p["team_pf"] + EPS))
        * 0.4
        * p["opp_fta"]
        * (1 - (p["opp_ft"] / (p["opp_fta"] + EPS))) ** 2
    )

    stops = stops_individual + stops_team_share
    stop_pct = (stops * p["team_mp"]) / (p["team_poss"] * p["mp"] + EPS)

    team_def_rtg = 100 * (p["opp_pts"] / (p["team_poss"] + EPS))
    opp_points_per_scoring_poss = p["opp_pts"] / (
        p["opp_fg"]
        + (1 - (1 - (p["opp_ft"] / (p["opp_fta"] + EPS))) ** 2) * p["opp_fta"] * 0.4
        + EPS
    )

    p["def_rtg"] = team_def_rtg + 0.2 * (
        100 * opp_points_per_scoring_poss * (1 - stop_pct) - team_def_rtg
    )
    p["net_rtg"] = p["off_rtg"] - p["def_rtg"]

    # True shooting: points per shooting possession, the standard 0.44
    # free-throw weighting. Not a rating, but cheap here and the PRD lists
    # it as a v1.5 stat-pair candidate.
    p["ts_pct"] = p["pts"] / (2 * (p["fga"] + 0.44 * p["fta"]) + EPS)

    p["min_per_game"] = p["mp"] / (p["gp"] + EPS)
    return p
