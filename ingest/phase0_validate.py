"""
Phase 0 validation script — proves the free data path works end to end.

Reads hoopR-nba-data box scores (fixtures/), derives team and player
offensive/defensive ratings from raw box score inputs (no pre-computed
ratings from the provider), and prints season-level results for manual
reconciliation against Basketball-Reference.

This is a throwaway validation script, not the Phase 1 ingest package.
Formulas here get moved into ingest/ modules once Phase 0 passes.
"""

import pandas as pd

SEASON = 2026
REGULAR_SEASON = 2  # season_type == 2


def load():
    pb = pd.read_parquet("fixtures/player_box_2026.parquet")
    tb = pd.read_parquet("fixtures/team_box_2026.parquet")
    pb = pb[pb["season_type"] == REGULAR_SEASON].copy()
    tb = tb[tb["season_type"] == REGULAR_SEASON].copy()

    # All-Star Weekend games (Team Stars / Team Stripes / World) are tagged
    # season_type == 2, same as real regular-season games — season_type
    # alone does not separate them out. Confirmed by inspection: the 30 real
    # franchises use ESPN team_id 1-30; exhibition teams use large synthetic
    # IDs (111386, 132374, 132375 observed). Filter on that. See MEMORY.md.
    real_team_ids = set(range(1, 31))
    tb = tb[tb["team_id"].isin(real_team_ids)].copy()
    pb = pb[pb["team_id"].isin(real_team_ids)].copy()
    return pb, tb


# ---------------------------------------------------------------------------
# Team ratings — trivial from box scores (DATA.md §2b / MODEL.md §8a)
# ---------------------------------------------------------------------------

def team_ratings(tb: pd.DataFrame) -> pd.DataFrame:
    tb = tb.copy()
    # total_turnovers = player turnovers + team-attributed turnovers (shot-clock etc).
    # Both end a possession, so the possessions formula needs the total, not
    # just `turnovers` (player-only). Confirmed via inspection — see MEMORY.md.
    #
    # Possessions use the precise two-sided estimate (averaging both teams'
    # per-game estimates, with the ORB%-weighted missed-FG term), not the
    # simplified `FGA + 0.44*FTA - OREB + TOV` from DATA.md/MODEL.md §8a.
    # The simplified formula is internally consistent (league ORtg == DRtg
    # exactly) but reconciles against Basketball-Reference with a systematic
    # ~2.5-3.5 pt offset on BOTH ORtg and DRtg (net rating stays close) —
    # confirmed 2025-26, see MEMORY.md reconciliation log. BRef's own team
    # ratings page uses this precise formula (it even footnotes that its
    # numbers differ from other pages on the site for this reason).
    opp_cols = tb[[
        "game_id", "team_id", "field_goals_made", "field_goals_attempted",
        "free_throws_attempted", "offensive_rebounds", "defensive_rebounds",
        "total_turnovers",
    ]].rename(columns={
        "team_id": "opponent_team_id",
        "field_goals_made": "opp_fg", "field_goals_attempted": "opp_fga",
        "free_throws_attempted": "opp_fta", "offensive_rebounds": "opp_orb",
        "defensive_rebounds": "opp_drb", "total_turnovers": "opp_tov",
    })
    tb = tb.merge(opp_cols, on=["game_id", "opponent_team_id"])

    tm_est = (
        tb["field_goals_attempted"] + 0.4 * tb["free_throws_attempted"]
        - 1.07 * (tb["offensive_rebounds"] / (tb["offensive_rebounds"] + tb["opp_drb"]))
        * (tb["field_goals_attempted"] - tb["field_goals_made"])
        + tb["total_turnovers"]
    )
    opp_est = (
        tb["opp_fga"] + 0.4 * tb["opp_fta"]
        - 1.07 * (tb["opp_orb"] / (tb["opp_orb"] + tb["defensive_rebounds"]))
        * (tb["opp_fga"] - tb["opp_fg"])
        + tb["opp_tov"]
    )
    tb["poss"] = 0.5 * (tm_est + opp_est)

    agg = tb.groupby("team_id").agg(
        team_name=("team_display_name", "first"),
        gp=("game_id", "nunique"),
        pts=("team_score", "sum"),
        poss=("poss", "sum"),
        opp_pts=("opponent_team_score", "sum"),
    ).reset_index()

    # Opponent possessions faced: self-join on game_id to get the opposing
    # team's own poss for that game, summed over the season.
    opp = tb[["game_id", "team_id", "poss"]].rename(
        columns={"team_id": "opponent_team_id", "poss": "opp_poss"}
    )
    merged = tb[["game_id", "team_id", "opponent_team_id"]].merge(
        opp, on=["game_id", "opponent_team_id"]
    )
    opp_poss_sum = merged.groupby("team_id")["opp_poss"].sum().reset_index()
    agg = agg.merge(opp_poss_sum, on="team_id")

    agg["off_rtg"] = 100 * agg["pts"] / agg["poss"]
    agg["def_rtg"] = 100 * agg["opp_pts"] / agg["opp_poss"]
    agg["net_rtg"] = agg["off_rtg"] - agg["def_rtg"]
    agg["pace"] = agg["poss"] / agg["gp"]
    return agg.sort_values("net_rtg", ascending=False)


# ---------------------------------------------------------------------------
# Player ratings — Dean Oliver individual ORtg/DRtg (MODEL.md §8a)
# Computed on season-aggregated totals, matching how Basketball-Reference
# derives its published advanced-stats table (not summed per-game ratings).
# ---------------------------------------------------------------------------

def build_team_totals(tb: pd.DataFrame) -> pd.DataFrame:
    t = tb.groupby("team_id").agg(
        team_mp=("game_id", "size"),  # placeholder, recomputed below
        team_fg=("field_goals_made", "sum"),
        team_fga=("field_goals_attempted", "sum"),
        team_3p=("three_point_field_goals_made", "sum"),
        team_ft=("free_throws_made", "sum"),
        team_fta=("free_throws_attempted", "sum"),
        team_orb=("offensive_rebounds", "sum"),
        team_drb=("defensive_rebounds", "sum"),
        team_ast=("assists", "sum"),
        team_tov=("total_turnovers", "sum"),
        team_pf=("fouls", "sum"),
        team_pts=("team_score", "sum"),
        team_blk=("blocks", "sum"),
        team_stl=("steals", "sum"),
    ).reset_index()
    return t


def build_opponent_totals(tb: pd.DataFrame) -> pd.DataFrame:
    """For each team, the season-summed box stats *of the teams it played
    against* — i.e. what opponents did in games against this team. Needed
    for the individual defensive rating formula."""
    self_stats = tb[[
        "game_id", "team_id", "field_goals_made", "field_goals_attempted",
        "free_throws_made", "free_throws_attempted", "offensive_rebounds",
        "defensive_rebounds", "total_turnovers", "team_score",
    ]].rename(columns={"team_id": "opponent_team_id"})
    joined = tb[["game_id", "team_id", "opponent_team_id"]].merge(
        self_stats, on=["game_id", "opponent_team_id"], suffixes=("", "_opp")
    )
    opp = joined.groupby("team_id").agg(
        opp_fg=("field_goals_made", "sum"),
        opp_fga=("field_goals_attempted", "sum"),
        opp_ft=("free_throws_made", "sum"),
        opp_fta=("free_throws_attempted", "sum"),
        opp_orb=("offensive_rebounds", "sum"),
        opp_drb=("defensive_rebounds", "sum"),
        opp_tov=("total_turnovers", "sum"),
        opp_pts=("team_score", "sum"),
    ).reset_index()
    return opp


def player_ratings(pb: pd.DataFrame, tb: pd.DataFrame, team_poss: pd.DataFrame) -> pd.DataFrame:
    # Player minutes come as "MM" or "MM:SS"-ish floats already in this
    # provider (a plain float column) — sum directly.
    p = pb.groupby(["athlete_id", "team_id"]).agg(
        player_name=("athlete_display_name", "first"),
        team_name=("team_short_display_name", "first"),
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
        tov=("turnovers", "sum"),  # individual turnovers, not team_turnovers
        pf=("fouls", "sum"),
        pts=("points", "sum"),
    ).reset_index()

    team_totals = build_team_totals(tb)
    # team_mp: sum of player minutes for that team (standard approach — 5x game minutes incl. OT)
    team_mp = pb.groupby("team_id")["minutes"].sum().reset_index().rename(columns={"minutes": "team_mp_real"})
    team_totals = team_totals.merge(team_mp, on="team_id")
    team_totals["team_mp"] = team_totals["team_mp_real"]

    opp_totals = build_opponent_totals(tb)
    team_totals = team_totals.merge(opp_totals, on="team_id")
    team_totals = team_totals.merge(team_poss[["team_id", "poss"]].rename(columns={"poss": "team_poss"}), on="team_id")

    p = p.merge(team_totals, on="team_id")

    eps = 1e-9

    # --- Offensive rating (Oliver) ---
    qAST = (
        (p["mp"] / (p["team_mp"] / 5)) * (1.14 * ((p["team_ast"] - p["ast"]) / (p["team_fg"] + eps)))
    ) + (
        (((p["team_ast"] / p["team_mp"]) * p["mp"] * 5 - p["ast"]) /
         ((p["team_fg"] / p["team_mp"]) * p["mp"] * 5 - p["fg"] + eps))
        * (1 - (p["mp"] / (p["team_mp"] / 5)))
    )
    qAST = qAST.clip(lower=0, upper=1.3).fillna(0)

    fg_part = p["fg"] * (1 - 0.5 * ((p["pts"] - p["ft"]) / (2 * p["fga"] + eps)) * qAST)
    ast_part = 0.5 * (((p["team_pts"] - p["team_ft"]) - (p["pts"] - p["ft"])) /
                       (2 * (p["team_fga"] - p["fga"] + eps))) * p["ast"]
    ft_part = (1 - (1 - (p["ft"] / (p["fta"] + eps))) ** 2) * 0.4 * p["fta"]

    team_scoring_poss = p["team_fg"] + (1 - (1 - (p["team_ft"] / (p["team_fta"] + eps))) ** 2) * p["team_fta"] * 0.4
    team_orb_pct = p["team_orb"] / (p["team_orb"] + p["opp_drb"] + eps)
    team_play_pct = team_scoring_poss / (p["team_fga"] + p["team_fta"] * 0.4 + p["team_tov"] + eps)
    team_orb_weight = ((1 - team_orb_pct) * team_play_pct) / (
        (1 - team_orb_pct) * team_play_pct + team_orb_pct * (1 - team_play_pct) + eps
    )

    orb_part = p["orb"] * team_orb_weight * team_play_pct

    sc_poss = (fg_part + ast_part + ft_part) * (
        1 - (p["team_orb"] / (team_scoring_poss + eps)) * team_orb_weight * team_play_pct
    ) + orb_part

    fgx_poss = (p["fga"] - p["fg"]) * (1 - 1.07 * team_orb_pct)
    ftx_poss = (1 - (p["ft"] / (p["fta"] + eps))) ** 2 * 0.4 * p["fta"]

    tot_poss = sc_poss + fgx_poss + ftx_poss + p["tov"]

    pprod_fg_part = 2 * (p["fg"] + 0.5 * p["tp"]) * (1 - 0.5 * ((p["pts"] - p["ft"]) / (2 * p["fga"] + eps)) * qAST)
    pprod_ast_part = 2 * (
        (p["team_fg"] - p["fg"] + 0.5 * (p["team_3p"] - p["tp"])) / (p["team_fg"] - p["fg"] + eps)
    ) * 0.5 * (((p["team_pts"] - p["team_ft"]) - (p["pts"] - p["ft"])) /
               (2 * (p["team_fga"] - p["fga"] + eps))) * p["ast"]
    pprod_orb_part = p["orb"] * team_orb_weight * team_play_pct * (p["team_pts"] / (team_scoring_poss + eps))

    pprod = (pprod_fg_part + pprod_ast_part + p["ft"]) * (
        1 - (p["team_orb"] / (team_scoring_poss + eps)) * team_orb_weight * team_play_pct
    ) + pprod_orb_part

    p["off_rtg"] = 100 * (pprod / (tot_poss + eps))
    p["poss_individual"] = tot_poss

    # --- Defensive rating (Oliver) ---
    dor_pct = p["opp_orb"] / (p["opp_orb"] + p["team_drb"] + eps)
    dfg_pct = p["opp_fg"] / (p["opp_fga"] + eps)
    fm_wt = (dfg_pct * (1 - dor_pct)) / (dfg_pct * (1 - dor_pct) + (1 - dfg_pct) * dor_pct + eps)

    stops1 = p["stl"] + p["blk"] * fm_wt * (1 - 1.07 * dor_pct) + p["drb"] * (1 - fm_wt)
    stops2 = (
        ((p["opp_fga"] - p["opp_fg"] - p["team_blk"]) / p["team_mp"]) * fm_wt * (1 - 1.07 * dor_pct)
        + ((p["opp_tov"] - p["team_stl"]) / p["team_mp"])
    ) * p["mp"] + (p["pf"] / (p["team_pf"] + eps)) * 0.4 * p["opp_fta"] * (1 - (p["opp_ft"] / (p["opp_fta"] + eps))) ** 2

    stops = stops1 + stops2
    stop_pct = (stops * p["team_mp"]) / (p["team_poss"] * p["mp"] + eps)

    team_def_rtg = 100 * (p["opp_pts"] / (p["team_poss"] + eps))
    d_pts_per_scposs = p["opp_pts"] / (p["opp_fg"] + (1 - (1 - (p["opp_ft"] / (p["opp_fta"] + eps))) ** 2) * p["opp_fta"] * 0.4 + eps)

    p["def_rtg"] = team_def_rtg + 0.2 * (100 * d_pts_per_scposs * (1 - stop_pct) - team_def_rtg)

    p["net_rtg"] = p["off_rtg"] - p["def_rtg"]
    return p


def main():
    pb, tb = load()
    print(f"Loaded {len(pb)} player-game rows, {len(tb)} team-game rows (regular season {SEASON}).")

    teams = team_ratings(tb)
    print("\n=== Team ratings (season) ===")
    print(teams[["team_name", "gp", "off_rtg", "def_rtg", "net_rtg", "pace"]].round(2).to_string(index=False))

    league_off = (teams["pts"].sum() / teams["poss"].sum()) * 100
    league_def = (teams["opp_pts"].sum() / teams["opp_poss"].sum()) * 100
    print(f"\nLeague-wide ORtg: {league_off:.2f}   League-wide DRtg: {league_def:.2f}")
    print("(sanity check — these should be ~equal, since one team's points scored is another's points allowed)")

    players = player_ratings(pb, tb, teams)
    # Minimum-sample floor purely to make the printed table readable —
    # NOT the shrinkage mechanism (that's MODEL.md's job in Phase 2).
    sample = players[players["mp"] >= 500].sort_values("off_rtg", ascending=False)
    print(f"\n=== Player ratings, {len(players)} player-team stints, {len(sample)} with 500+ minutes ===")
    cols = ["player_name", "team_name", "gp", "mp", "pts", "off_rtg", "def_rtg", "net_rtg"]
    print(sample[cols].round(1).head(20).to_string(index=False))

    players.to_csv("fixtures/player_ratings_2026_derived.csv", index=False)
    teams.to_csv("fixtures/team_ratings_2026_derived.csv", index=False)
    print("\nWrote fixtures/player_ratings_2026_derived.csv and fixtures/team_ratings_2026_derived.csv")


if __name__ == "__main__":
    main()
