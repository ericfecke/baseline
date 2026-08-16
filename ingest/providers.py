"""
Provider abstraction (DATA.md §4).

All external stats access goes behind `StatsProvider`. Everything
downstream — validation, transactions resolution, ratings, QA, publish —
is written against this interface and cannot tell which implementation
ran. That is what makes swapping to a licensed provider a one-file
change if the free path ever dies.

Note this returns *box scores*, not ratings. Ratings are derived in
`ingest/ratings.py`, deliberately: box scores are commodity data,
ratings are the scarce paywalled part, so we compute the scarce thing
ourselves and any provider with a box score works (DATA.md §2b).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd
import requests

HOOPR_RAW_BASE = (
    "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/main/nba"
)

# A real season file is hundreds of KB. Anything much smaller is an error
# page or a truncated download masquerading as parquet — fail on it here
# rather than letting pyarrow raise something inscrutable three stages
# later. Learned the hard way in Phase 0 CI; see MEMORY.md.
MIN_PLAUSIBLE_PARQUET_BYTES = 10_000

FETCH_TIMEOUT_SECONDS = 60


class ProviderError(RuntimeError):
    """Raised when a provider cannot supply usable data.

    Distinct from a validation failure: this means we never got the data,
    not that the data was wrong.
    """


@dataclass(frozen=True)
class ProviderData:
    """Raw, unvalidated data straight from a provider.

    Named for what it is rather than `BoxScores`, since it now carries
    three different things the pipeline needs:

    * `player_box` / `team_box` — the box scores everything is derived from.
    * `schedule` — per-game type metadata the box scores lack. This is what
      lets us exclude the NBA Cup Championship by its actual label rather
      than guessing from the date (`stages/normalize.py`).
    * `player_core` — player bio including draft year, which is the only
      acquisition signal any free source gives us
      (`stages/transactions.py`).

    `provider` names the implementation that produced these so it can be
    stamped into the snapshot's `meta` and the run log — when a number
    looks wrong six months from now, the first question is which source it
    came from.
    """

    provider: str
    season: int
    player_box: pd.DataFrame
    team_box: pd.DataFrame
    schedule: pd.DataFrame
    player_core: pd.DataFrame


# Kept so the old name doesn't break anything that imported it.
BoxScores = ProviderData


@runtime_checkable
class StatsProvider(Protocol):
    """The one interface. Implementations must be swappable without any
    downstream stage noticing."""

    name: str

    def get_box_scores(self, season: int) -> ProviderData: ...


class HoopRProvider:
    """The Phase 0-validated path: `sportsdataverse/hoopR-nba-data`.

    Reads parquet straight from GitHub raw. Crucially this never touches
    stats.nba.com, so the datacenter-IP block that rules out cloud-hosted
    `nba_api` never applies and the nightly job can run on GitHub Actions
    (DATA.md §2a). Data is ESPN-sourced and CC BY 4.0 licensed.

    Season numbering follows the NBA convention of the year the season
    *ends*: the 2025-26 season is `2026`.
    """

    name = "hoopr"

    def __init__(self, cache_dir: Path | None = None) -> None:
        # Caching keeps repeat local runs off the network entirely, which
        # matters because idempotency testing means running this a lot.
        self.cache_dir = Path(cache_dir) if cache_dir else Path("fixtures")

    def _url(self, kind: str, season: int) -> str:
        # The schedule directory breaks the otherwise-uniform naming:
        # nba/schedules/parquet/nba_schedule_{season}.parquet, not
        # nba/schedule/parquet/schedule_{season}.parquet. Everything else
        # (player_box, team_box, player_core) follows {kind}/{kind}_{season}.
        if kind == "schedule":
            return f"{HOOPR_RAW_BASE}/schedules/parquet/nba_schedule_{season}.parquet"
        return f"{HOOPR_RAW_BASE}/{kind}/parquet/{kind}_{season}.parquet"

    def _fetch_parquet(self, kind: str, season: int) -> pd.DataFrame:
        target = self.cache_dir / f"{kind}_{season}.parquet"

        if not target.exists():
            url = self._url(kind, season)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ProviderError(f"fetching {url} failed: {exc}") from exc

            if len(response.content) < MIN_PLAUSIBLE_PARQUET_BYTES:
                raise ProviderError(
                    f"{url} returned only {len(response.content)} bytes — "
                    "expected a parquet file of at least "
                    f"{MIN_PLAUSIBLE_PARQUET_BYTES}. Probably an error page."
                )

            # Write via a temp file so an interrupted run can't leave a
            # half-written parquet in the cache to be trusted next time.
            tmp = target.with_suffix(".parquet.partial")
            tmp.write_bytes(response.content)
            tmp.replace(target)

        try:
            return pd.read_parquet(target)
        except Exception as exc:
            raise ProviderError(
                f"{target} exists but is not readable parquet ({exc}). "
                "Delete it and retry to force a fresh download."
            ) from exc

    def get_box_scores(self, season: int) -> ProviderData:
        return ProviderData(
            provider=self.name,
            season=season,
            player_box=self._fetch_parquet("player_box", season),
            team_box=self._fetch_parquet("team_box", season),
            schedule=self._fetch_parquet("schedule", season),
            player_core=self._fetch_parquet("player_core", season),
        )


class FixtureProvider:
    """Local fixtures — tests and offline development (DATA.md §4).

    Reads only from disk and never touches the network, so tests are
    deterministic and CI can exercise the pipeline against a known-bad
    payload without inventing a fake HTTP server.
    """

    name = "fixture"

    def __init__(self, fixture_dir: Path | str = "fixtures") -> None:
        self.fixture_dir = Path(fixture_dir)

    def get_box_scores(self, season: int) -> ProviderData:
        paths = {
            kind: self.fixture_dir / f"{kind}_{season}.parquet"
            for kind in ("player_box", "team_box", "schedule", "player_core")
        }
        missing = [str(p) for p in paths.values() if not p.exists()]
        if missing:
            raise ProviderError(
                "missing fixture(s): "
                + ", ".join(missing)
                + ". Run the hoopR provider once to populate them."
            )
        return ProviderData(
            provider=self.name,
            season=season,
            player_box=pd.read_parquet(paths["player_box"]),
            team_box=pd.read_parquet(paths["team_box"]),
            schedule=pd.read_parquet(paths["schedule"]),
            player_core=pd.read_parquet(paths["player_core"]),
        )


PROVIDERS: dict[str, type] = {
    "hoopr": HoopRProvider,
    "fixture": FixtureProvider,
}


def get_provider(name: str) -> StatsProvider:
    """Resolve a provider by name.

    Deliberately explicit rather than dynamic import: the set of trusted
    providers is small and knowing exactly which ones exist is worth more
    than pluggability we don't need.
    """
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}. Available: {sorted(PROVIDERS)}"
        ) from None
