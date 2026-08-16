"""
Pydantic boundary models — the contract with whatever provider ran.

"External data lies" (DATA.md §5). Every row a provider hands us passes
through here before any arithmetic touches it.

Division of labour between this module and the QA stage (MODEL.md §6):

  * Here (per-row, structural): types, impossible values, and internal
    consistency a single row can contradict on its own — negative
    rebounds, made > attempted, 300 minutes in one game.
  * QA stage (aggregate): things only visible across rows or across runs
    — row counts vs. the prior run, team totals not reconciling with the
    sum of their players, a rating jumping day over day.

A row that fails here fails the run. Known, characterised provider
quirks (e.g. hoopR's null-`athlete_id` rows) are filtered upstream in
the normalize stage with a counted reason — see `stages/normalize.py`.
The rule is: quirks we understand get filtered and counted, anything
else is loud.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 48 regulation + 5 OT periods of headroom. This guards against unit
# errors and typos (a "300 minute" game), not against real overtime.
MAX_MINUTES_PER_GAME = 73.0

# ESPN season_type codes as observed in hoopR data.
SEASON_TYPE_PRESEASON = 1
SEASON_TYPE_REGULAR = 2
SEASON_TYPE_POSTSEASON = 3
SEASON_TYPE_PLAY_IN = 5

# The 30 real franchises use ESPN team_id 1-30. All-Star / exhibition
# squads use large synthetic IDs and are tagged season_type == 2 exactly
# like real games, so this range is the only reliable filter. See
# MEMORY.md — this bit us in Phase 0.
REAL_TEAM_IDS = frozenset(range(1, 31))

# ESPN game `type_id` values that appear under season_type == 2 but do NOT
# count toward regular-season statistics:
#
#   4  ALLSTAR -- All-Star Weekend exhibitions
#   39 CC      -- NBA Cup (In-Season Tournament) Championship game
#
# The Cup Championship is the subtle one, and it matches the NBA's own rule:
# every other Cup game — group play, quarterfinals, semifinals — counts as a
# regular-season game and is typed STD, but the final does not. ESPN still
# ships it as season_type 2, which is why Basketball-Reference shows San
# Antonio and New York at 82 games while the raw feed has them at 83.
# See MEMORY.md.
NON_COUNTING_GAME_TYPE_IDS = frozenset({4, 39})


class _StrictRow(BaseModel):
    """Providers hand us dozens of columns; we ignore the ones we don't use,
    but never silently accept a malformed value in one we do."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _nan_to_none(cls, data):
        """Translate pandas' missing-value representation into Python's.

        pandas stores a missing float as `NaN`, which is itself a float —
        so it satisfies `Optional[float]` but then fails every `ge=0`
        bound, producing a wall of confusing errors for what is really
        just "this player didn't play". Converting at the boundary is
        this layer's job: the provider's representation stops here and
        our domain types start.
        """
        if isinstance(data, dict):
            return {
                key: (
                    None
                    if isinstance(value, float) and math.isnan(value)
                    else value
                )
                for key, value in data.items()
            }
        return data


class PlayerBoxRow(_StrictRow):
    """One player's line in one game.

    Every counting stat is Optional: hoopR writes nulls across the whole
    box line for a player who didn't play (DNP-rest, inactive, etc.).
    A null line is valid data meaning "no line", not a broken row — but
    `minutes is None` is exactly how we decide the game doesn't count
    toward `gp`, so it must survive validation rather than default to 0.
    """

    game_id: int
    season: int
    season_type: int
    game_date: date

    athlete_id: int
    athlete_display_name: str
    athlete_position_abbreviation: Optional[str] = None
    athlete_jersey: Optional[str] = None

    team_id: int
    opponent_team_id: int
    team_score: int = Field(ge=0)
    opponent_team_score: int = Field(ge=0)
    home_away: Literal["home", "away"]

    did_not_play: bool

    minutes: Optional[float] = Field(default=None, ge=0, le=MAX_MINUTES_PER_GAME)
    field_goals_made: Optional[float] = Field(default=None, ge=0)
    field_goals_attempted: Optional[float] = Field(default=None, ge=0)
    three_point_field_goals_made: Optional[float] = Field(default=None, ge=0)
    three_point_field_goals_attempted: Optional[float] = Field(default=None, ge=0)
    free_throws_made: Optional[float] = Field(default=None, ge=0)
    free_throws_attempted: Optional[float] = Field(default=None, ge=0)
    offensive_rebounds: Optional[float] = Field(default=None, ge=0)
    defensive_rebounds: Optional[float] = Field(default=None, ge=0)
    assists: Optional[float] = Field(default=None, ge=0)
    steals: Optional[float] = Field(default=None, ge=0)
    blocks: Optional[float] = Field(default=None, ge=0)
    turnovers: Optional[float] = Field(default=None, ge=0)
    fouls: Optional[float] = Field(default=None, ge=0)
    points: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _made_not_exceeding_attempted(self) -> "PlayerBoxRow":
        for made, att in (
            ("field_goals_made", "field_goals_attempted"),
            ("three_point_field_goals_made", "three_point_field_goals_attempted"),
            ("free_throws_made", "free_throws_attempted"),
        ):
            m, a = getattr(self, made), getattr(self, att)
            if m is not None and a is not None and m > a:
                raise ValueError(
                    f"{made}={m} exceeds {att}={a} "
                    f"(athlete_id={self.athlete_id}, game_id={self.game_id})"
                )
        return self

    @model_validator(mode="after")
    def _threes_not_exceeding_field_goals(self) -> "PlayerBoxRow":
        if (
            self.three_point_field_goals_made is not None
            and self.field_goals_made is not None
            and self.three_point_field_goals_made > self.field_goals_made
        ):
            raise ValueError(
                f"three_point_field_goals_made={self.three_point_field_goals_made} "
                f"exceeds field_goals_made={self.field_goals_made} "
                f"(athlete_id={self.athlete_id}, game_id={self.game_id})"
            )
        return self

    @property
    def played(self) -> bool:
        """Whether this game counts toward games-played.

        `minutes is not None` is the reliable signal, verified against
        Basketball-Reference: Rudy Gobert has 79 rows in the 2025-26
        regular season but 76 with a box line, and BRef reports G=76.
        Do NOT use the `active` column for this — it tracks roster
        status, not participation (Gobert: active=True on only 14 of
        those 76 games). See MEMORY.md.
        """
        return self.minutes is not None


class TeamBoxRow(_StrictRow):
    """One team's line in one game."""

    game_id: int
    season: int
    season_type: int
    game_date: date

    team_id: int
    team_display_name: str
    team_abbreviation: str
    team_color: Optional[str] = None
    team_alternate_color: Optional[str] = None

    opponent_team_id: int
    team_score: int = Field(ge=0)
    opponent_team_score: int = Field(ge=0)
    team_home_away: Literal["home", "away"]

    field_goals_made: float = Field(ge=0)
    field_goals_attempted: float = Field(ge=0)
    three_point_field_goals_made: float = Field(ge=0)
    three_point_field_goals_attempted: float = Field(ge=0)
    free_throws_made: float = Field(ge=0)
    free_throws_attempted: float = Field(ge=0)
    offensive_rebounds: float = Field(ge=0)
    defensive_rebounds: float = Field(ge=0)
    assists: float = Field(ge=0)
    steals: float = Field(ge=0)
    blocks: float = Field(ge=0)
    fouls: float = Field(ge=0)

    # hoopR splits turnovers three ways. `total_turnovers` is the one the
    # possessions formula needs — a team-charged turnover (shot clock,
    # backcourt) ends a possession just as a player's does. Using bare
    # `turnovers` systematically undercounts possessions. See MEMORY.md.
    #
    # `team_turnovers` is deliberately NOT bounded at >= 0. It is a derived
    # residual (`total_turnovers` minus the sum of player turnovers, which
    # holds in 2458/2462 rows of 2025-26), so when ESPN under-reports the
    # team total it goes negative. Rejecting negatives here would kill an
    # entire nightly run over 4 bad upstream rows; the real impossibility
    # (`total_turnovers < turnovers`) is repaired and counted in the
    # normalize stage instead.
    turnovers: float = Field(ge=0)
    team_turnovers: float
    total_turnovers: float = Field(ge=0)

    @model_validator(mode="after")
    def _made_not_exceeding_attempted(self) -> "TeamBoxRow":
        for made, att in (
            ("field_goals_made", "field_goals_attempted"),
            ("three_point_field_goals_made", "three_point_field_goals_attempted"),
            ("free_throws_made", "free_throws_attempted"),
        ):
            m, a = getattr(self, made), getattr(self, att)
            if m > a:
                raise ValueError(
                    f"{made}={m} exceeds {att}={a} "
                    f"(team_id={self.team_id}, game_id={self.game_id})"
                )
        return self

    @model_validator(mode="after")
    def _turnovers_reconcile(self) -> "TeamBoxRow":
        """The three turnover columns must be self-consistent.

        This holds in every row of 2025-26 including the four with a
        negative residual, because the negative is precisely what keeps
        the identity true. If this ever breaks, the provider changed the
        meaning of one of these columns and the possessions formula needs
        re-deriving — which is worth failing a run over.
        """
        expected = self.turnovers + self.team_turnovers
        if abs(self.total_turnovers - expected) > 0.5:
            raise ValueError(
                f"total_turnovers={self.total_turnovers} != turnovers"
                f"({self.turnovers}) + team_turnovers({self.team_turnovers}) "
                f"(team_id={self.team_id}, game_id={self.game_id})"
            )
        return self

    @property
    def possession_turnovers(self) -> float:
        """Turnovers to use in the possessions estimate, repaired if needed.

        `total_turnovers` can be under-reported upstream — in 2025-26 four
        rows had it *below* the player-attributable `turnovers`, which is
        impossible (a team total cannot be less than its parts). The worst
        case, game 401810469, reported 0 total turnovers for both teams
        while their players recorded 16 and 12.

        Taking the max repairs the floor without inventing data: we never
        claim more turnovers than one of the two sources reports. Callers
        should count how often `turnovers_repaired` is True and surface it
        as a QA flag rather than repairing silently.
        """
        return max(self.total_turnovers, self.turnovers)

    @property
    def turnovers_repaired(self) -> bool:
        return self.total_turnovers < self.turnovers


# ---------------------------------------------------------------------------
# Published snapshot schema (DATA.md §7)
#
# The other half of the contract: what the web app is promised. Modelling
# the output as well as the input means the publish stage can't silently
# change shape, and the JSON schema is documented by construction.
#
# Deliberately absent: any wall-clock field. The snapshot is a pure
# function of (input data, as_of_date) so "running ingest twice changes
# nothing" is a byte comparison (DATA.md §5). Run timestamps live in the
# append-only run log instead.
# ---------------------------------------------------------------------------


class TeamRating(_StrictRow):
    provider_id: int
    name: str
    abbr: str
    conference: Optional[str] = None
    division: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None

    gp: int = Field(ge=0)
    poss: float = Field(ge=0)
    off_rtg: float
    def_rtg: float
    net_rtg: float
    pace: float = Field(ge=0)


class PlayerRating(_StrictRow):
    provider_id: int
    name: str
    team_provider_id: int
    position: Optional[str] = None
    jersey: Optional[str] = None

    stint_id: str

    gp: int = Field(ge=0)
    min_total: float = Field(ge=0)
    min_per_game: float = Field(ge=0)
    poss: float = Field(ge=0)

    off_rtg: float
    def_rtg: float
    net_rtg: float
    ts_pct: Optional[float] = None


class Stint(_StrictRow):
    """A contiguous span of games with one team (DATA.md §6a).

    `poss` here is what becomes `n` in Phase 2's shrinkage weight — a
    trade resets the sample because it resets the context.
    """

    stint_id: str
    player_provider_id: int
    team_provider_id: int
    season: int
    start_date: date
    end_date: date
    # Only values the pipeline can actually justify from available data.
    #
    #   season_start -- first stint of the season, established player
    #   rookie_debut -- first stint, drafted for this season
    #   team_change  -- moved from another NBA team mid-season
    #   unknown      -- fallback
    #
    # Notably absent: trade, signing, waiver, buyout, g_league_callup.
    # Those describe the *mechanism* of a move, and no free source in play
    # carries it — hoopR ships no transactions feed (its `rosters` file is a
    # current snapshot with no dates or history, and `game_rosters.reason`
    # holds injury text). `team_change` is the honest limit of what a game
    # log supports. A fan reading "Traded to Phoenix" would believe it, so
    # we don't say it unless we know it. See DATA.md §6a.
    acquisition_type: Literal[
        "season_start",
        "rookie_debut",
        "team_change",
        "unknown",
    ]
    boundary_source: Literal[
        "season_start", "game_log_inference", "draft_data", "roster_data"
    ]
    gp: int = Field(ge=0)
    poss: float = Field(ge=0)


class QaResult(_StrictRow):
    confidence: float = Field(ge=0, le=1)
    flags: list[dict] = Field(default_factory=list)
    passed: bool


class SnapshotMeta(_StrictRow):
    season: int
    as_of_date: date
    provider: str
    qa: QaResult
    counts: dict[str, int]


class Snapshot(_StrictRow):
    meta: SnapshotMeta
    teams: list[TeamRating]
    players: list[PlayerRating]
    stints: list[Stint]
