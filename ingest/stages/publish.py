"""
publish — write the dated JSON snapshot (DATA.md §7).

Two rules make Phase 1's acceptance criterion ("running ingest twice
produces identical state") a *byte* comparison rather than a hopeful one:

1. **No wall-clock data in the payload.** No `generated_at`, no run id, no
   duration. Those go in the run log, which is history rather than state.
2. **Deterministic ordering and rounding everywhere.** Rows sort by a
   stable key, dict keys are sorted, floats are rounded to a fixed number
   of places. Without the rounding, the last bits of a float can differ
   between platforms and the byte comparison becomes a coin flip.

A QA failure writes nothing. The last good snapshot keeps serving and the
maintainer gets a loud non-zero exit (DATA.md §5).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from ..state import PipelineState

STAGE = "publish"

DATA_ROOT = Path("data")

# Ratings are meaningful to about a tenth of a point; possessions and
# minutes to a whole unit. Six places is far more precision than the data
# supports and still kills cross-platform float jitter.
FLOAT_PLACES = 6


class QaRejection(RuntimeError):
    """QA scored below threshold; nothing was written."""


def _clean(value: Any) -> Any:
    """Convert numpy/pandas scalars into plain JSON-safe Python.

    `json.dumps` cannot serialise numpy scalars, and pandas' NA sentinels
    are not `None`. Normalising here keeps the payload free of provider- and
    library-specific representations.
    """
    if value is None or value is pd.NaT:
        return None
    if hasattr(value, "item"):  # numpy scalar -> native Python
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, FLOAT_PLACES)
    if hasattr(value, "isoformat"):  # date / datetime
        return value.isoformat()
    return value


def _records(frame: pd.DataFrame, columns: dict[str, str], sort_by: str) -> list[dict]:
    """Project, rename, sort, and clean — in that order.

    `columns` maps source column -> published field name, so the published
    schema is explicit here rather than implied by whatever the dataframe
    happens to be carrying.
    """
    present = {src: dst for src, dst in columns.items() if src in frame.columns}
    projected = frame[list(present)].rename(columns=present)
    projected = projected.sort_values(sort_by, kind="mergesort")
    return [
        {key: _clean(value) for key, value in record.items()}
        for record in projected.to_dict("records")
    ]


TEAM_COLUMNS = {
    "team_id": "provider_id",
    "name": "name",
    "abbr": "abbr",
    "primary_color": "primary_color",
    "secondary_color": "secondary_color",
    "gp": "gp",
    "poss": "poss",
    "off_rtg": "off_rtg",
    "def_rtg": "def_rtg",
    "net_rtg": "net_rtg",
    "pace": "pace",
}

PLAYER_COLUMNS = {
    "athlete_id": "provider_id",
    "name": "name",
    "team_id": "team_provider_id",
    "position": "position",
    "jersey": "jersey",
    "stint_id": "stint_id",
    "gp": "gp",
    "mp": "min_total",
    "min_per_game": "min_per_game",
    "poss": "poss",
    "off_rtg": "off_rtg",
    "def_rtg": "def_rtg",
    "net_rtg": "net_rtg",
    "ts_pct": "ts_pct",
}

STINT_COLUMNS = {
    "stint_id": "stint_id",
    "athlete_id": "player_provider_id",
    "team_id": "team_provider_id",
    "start_date": "start_date",
    "end_date": "end_date",
    "acquisition_type": "acquisition_type",
    "boundary_source": "boundary_source",
    "gp": "gp",
    "poss": "poss",
}


def build_snapshot(state: PipelineState) -> dict[str, Any]:
    """Assemble the payload. Pure — no I/O, so tests can compare structures."""
    if state.team_ratings is None or state.player_ratings is None:
        raise ValueError("publish requires aggregated ratings")
    if state.as_of_date is None:
        raise ValueError("publish requires as_of_date")

    stints = state.stints
    if stints is not None and not stints.empty:
        # Stint possessions come from the rated player rows, since that is
        # where Oliver's individual possession estimate is computed.
        stint_poss = state.player_ratings.set_index("stint_id")["poss"]
        stints = stints.copy()
        stints["poss"] = stints["stint_id"].map(stint_poss).fillna(0.0)
        stints["season"] = state.season

    teams = _records(state.team_ratings, TEAM_COLUMNS, "provider_id")
    players = _records(state.player_ratings, PLAYER_COLUMNS, "provider_id")
    stint_records = (
        _records(stints, STINT_COLUMNS, "stint_id")
        if stints is not None and not stints.empty
        else []
    )

    return {
        "meta": {
            "season": state.season,
            "as_of_date": state.as_of_date.isoformat(),
            "provider": state.provider_name,
            "qa": {
                "confidence": state.qa_confidence,
                "passed": state.qa_passed,
                "flags": list(state.qa_flags),
            },
            "notes": [note.as_dict() for note in state.notes],
            "counts": {
                "teams": len(teams),
                "players": len(players),
                "stints": len(stint_records),
            },
        },
        "teams": teams,
        "players": players,
        "stints": stint_records,
    }


def serialize(snapshot: dict[str, Any]) -> str:
    """Canonical JSON. `sort_keys` plus a fixed separator and trailing
    newline means the same data always produces the same bytes."""
    return (
        json.dumps(snapshot, sort_keys=True, indent=2, separators=(",", ": "),
                   ensure_ascii=False)
        + "\n"
    )


def run(state: PipelineState, data_root: Path | None = None) -> PipelineState:
    root = Path(data_root) if data_root else DATA_ROOT

    if not state.qa_passed:
        errors = [f["detail"] for f in state.qa_flags if f["severity"] == "error"]
        raise QaRejection(
            f"QA confidence {state.qa_confidence} below threshold or errors "
            f"present; nothing written, last good snapshot still serving.\n"
            + "\n".join(f"  - {detail}" for detail in errors)
        )

    snapshot = build_snapshot(state)
    payload = serialize(snapshot)

    season_dir = root / "snapshots" / str(state.season)
    season_dir.mkdir(parents=True, exist_ok=True)
    (season_dir / f"{state.as_of_date.isoformat()}.json").write_text(
        payload, encoding="utf-8"
    )
    (root / "latest.json").write_text(payload, encoding="utf-8")

    return state.with_note(
        STAGE, "snapshot_written", 1,
        f"data/snapshots/{state.season}/{state.as_of_date.isoformat()}.json "
        f"({len(payload)} bytes)",
    )
