"""
orchestrator — compose the stages, log the run.

The one place that knows the pipeline order. Stages don't import each
other, so changing the sequence is an edit here and nowhere else.

Run logging is append-only JSONL. This is the file the UI's "Data as of ..."
stamp reads (DATA.md §5), and it is also the only place wall-clock time
appears — keeping it out of the snapshot is what makes the snapshot
byte-reproducible.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .state import PipelineState
from .stages import aggregate, fetch, normalize, publish, qa, transactions, validate

# Order matters. validate sits *after* normalize on purpose — see the
# module docstring in stages/validate.py for why (in short: validate the
# population the math actually consumes, not rows we were always going to
# discard).
PIPELINE: list[tuple[str, Callable[[PipelineState], PipelineState]]] = [
    ("fetch", fetch.run),
    ("normalize", normalize.run),
    ("validate", validate.run),
    ("transactions", transactions.run),
    ("aggregate", aggregate.run),
    ("qa", qa.run),
    ("publish", publish.run),
]

DATA_ROOT = Path("data")
RUN_LOG_NAME = "ingest_runs.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_run_log(entry: dict[str, Any], data_root: Path | None = None) -> None:
    root = Path(data_root) if data_root else DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    with (root / RUN_LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def run_pipeline(
    season: int,
    provider_name: str = "hoopr",
    as_of_date: date | None = None,
    data_root: Path | None = None,
    verbose: bool = True,
) -> tuple[PipelineState, dict[str, Any]]:
    """Execute every stage. Returns the final state and the run-log entry.

    Raises on failure after logging it — the caller decides the exit code,
    but the failure is always recorded first so a crashed nightly run is
    still visible in the log rather than being inferred from silence.
    """
    started_at = _utc_now_iso()
    state = PipelineState(
        season=season,
        provider_name=provider_name,
        as_of_date=as_of_date or date.today(),
    )

    def log_entry(status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
            "provider": provider_name,
            "season": season,
            "as_of_date": state.as_of_date.isoformat() if state.as_of_date else None,
            "status": status,
            "rows_written": {
                "teams": 0 if state.team_ratings is None else len(state.team_ratings),
                "players": (
                    0 if state.player_ratings is None else len(state.player_ratings)
                ),
                "stints": 0 if state.stints is None else len(state.stints),
            },
            "qa_confidence": state.qa_confidence,
            "qa_passed": state.qa_passed,
            "qa_flags": list(state.qa_flags),
            "notes": [note.as_dict() for note in state.notes],
            "error": error,
        }

    for stage_name, stage_fn in PIPELINE:
        try:
            if stage_name == "publish":
                state = stage_fn(state, data_root=data_root)
            else:
                state = stage_fn(state)
        except Exception as exc:
            entry = log_entry("failed", f"{stage_name}: {exc}")
            append_run_log(entry, data_root)
            if verbose:
                print(f"FAILED at stage '{stage_name}': {exc}", file=sys.stderr)
                traceback.print_exc()
            raise
        if verbose:
            print(f"  {stage_name:14s} ok")

    entry = log_entry("success")
    append_run_log(entry, data_root)
    return state, entry
