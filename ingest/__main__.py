"""
CLI entry point:  python -m ingest [--season 2026] [--provider hoopr]

Thin by design — argument parsing and exit codes only, no business logic.
Same discipline as xml-auditor's `app.py` (CLAUDE.md).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from .orchestrator import run_pipeline
from .providers import PROVIDERS

# GitHub's Ubuntu runners don't reliably hand Python a UTF-8 stdout, and a
# single non-ASCII character in output is then enough to crash the run. This
# cost four CI runs in Phase 0 — see MEMORY.md.
sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SEASON = 2026


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ingest",
        description="Baseline NBA ingest: box scores in, rated snapshot out.",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=DEFAULT_SEASON,
        help=f"season by ending year, e.g. 2026 for 2025-26 (default {DEFAULT_SEASON})",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default="hoopr",
        help="data source (default hoopr)",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_date,
        default=None,
        help="snapshot date, YYYY-MM-DD (default today). Set explicitly to "
        "reproduce a past snapshot byte-for-byte.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="where to write snapshots (default ./data)",
    )
    args = parser.parse_args(argv)

    print(f"Baseline ingest — season {args.season}, provider {args.provider}")
    try:
        state, entry = run_pipeline(
            season=args.season,
            provider_name=args.provider,
            as_of_date=args.as_of,
            data_root=args.data_root,
        )
    except Exception:
        # The orchestrator already logged and printed the detail. Exit
        # non-zero so CI fails loudly rather than reporting a green run that
        # wrote nothing.
        return 1

    print(
        f"\nQA confidence {state.qa_confidence} "
        f"({'passed' if state.qa_passed else 'FAILED'})"
    )
    for flag in state.qa_flags:
        print(f"  [{flag['severity']}] {flag['check']}: {flag['detail']}")

    counts = entry["rows_written"]
    print(
        f"\nWrote {counts['teams']} teams, {counts['players']} player stints, "
        f"{counts['stints']} stints"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
