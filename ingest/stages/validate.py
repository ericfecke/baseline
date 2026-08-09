"""
validate — every row through Pydantic before any arithmetic. "External
data lies" (DATA.md §5).

**Runs after normalize, not before.** DATA.md originally specified
`fetch -> validate -> normalize`; Phase 1 swapped the middle two and the
doc was updated to match. The reason is concrete: the provider legitimately
ships rows outside the population we compute on — 33 rows with a null
`athlete_id` (unkeyable), and All-Star exhibition squads tagged as regular
season. Validating before selection means failing the run over rows we
were always going to discard. Validating after means the guarantee is the
one that actually matters: *everything the math touches has been checked*.

A failure here is fatal by design. Characterised quirks were already
filtered and counted upstream, so anything reaching this point is a shape
we do not understand — and a wrong number rendered beautifully is worse
than no chart (MODEL.md §8a).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import ValidationError

from ..models import PlayerBoxRow, TeamBoxRow
from ..state import PipelineState

STAGE = "validate"

# How many bad rows to name in the exception. Enough to see a pattern,
# few enough to stay readable in a CI log.
MAX_REPORTED_FAILURES = 5


class BoundaryValidationError(RuntimeError):
    """Provider data failed the boundary contract."""


def _validate_frame(frame: pd.DataFrame, model: type, label: str) -> int:
    failures: list[str] = []

    for position, record in enumerate(frame.to_dict("records")):
        try:
            model.model_validate(record)
        except ValidationError as exc:
            if len(failures) < MAX_REPORTED_FAILURES:
                failures.append(_describe(label, position, record, exc))

    if failures:
        raise BoundaryValidationError(
            f"{label}: rows failed boundary validation.\n\n"
            + "\n\n".join(failures)
            + "\n\nThis is provider data in a shape we do not recognise. "
            "Either the upstream schema changed, or a genuinely bad value "
            "arrived. Fix the cause — do not relax the model to make this "
            "pass."
        )
    return len(frame)


def _describe(
    label: str, position: int, record: dict[str, Any], exc: ValidationError
) -> str:
    """Build an error message someone can act on without a debugger."""
    identity = ", ".join(
        f"{key}={record.get(key)!r}"
        for key in ("game_id", "game_date", "team_id", "athlete_id",
                    "athlete_display_name")
        if key in record
    )
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in exc.errors()[:MAX_REPORTED_FAILURES]
    )
    return f"  [{label} row {position}] {identity}\n    -> {problems}"


def run(state: PipelineState) -> PipelineState:
    if state.player_box is None or state.team_box is None:
        raise ValueError("validate requires normalized box scores; run normalize first")

    players = _validate_frame(state.player_box, PlayerBoxRow, "player_box")
    teams = _validate_frame(state.team_box, TeamBoxRow, "team_box")

    return state.with_note(
        STAGE, "rows_validated", players + teams,
        f"{players} player-game rows, {teams} team-game rows",
    )
