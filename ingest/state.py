"""
Pipeline state passed between stages.

Mirrors xml-auditor's convention — each stage receives the state and
returns an updated *copy*, never mutating in place. xml-auditor used a
plain dict with a documented schema; this uses a frozen dataclass, which
gets the same discipline enforced by the language instead of by comment,
and makes a typo in a key name a failure rather than a silent `None`.

`notes` is the counted-quirk channel. Anything we filter or repair on
purpose lands there with a count, so the QA stage can surface it and a
human can see what the pipeline decided to tolerate. Silent cleaning is
how you lose trust in a number six months later.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class Note:
    """A deliberate, counted intervention in the data."""

    stage: str
    kind: str
    count: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "kind": self.kind,
            "count": self.count,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PipelineState:
    season: int
    provider_name: str
    as_of_date: Optional[date] = None

    # Raw, straight from the provider.
    raw_player_box: Optional[pd.DataFrame] = None
    raw_team_box: Optional[pd.DataFrame] = None
    raw_schedule: Optional[pd.DataFrame] = None
    raw_player_core: Optional[pd.DataFrame] = None

    # Normalized: real teams only, one season type, int IDs, repaired
    # turnovers, `played` computed.
    player_box: Optional[pd.DataFrame] = None
    team_box: Optional[pd.DataFrame] = None

    # Derived.
    stints: Optional[pd.DataFrame] = None
    team_ratings: Optional[pd.DataFrame] = None
    player_ratings: Optional[pd.DataFrame] = None

    # QA verdict.
    qa_confidence: Optional[float] = None
    qa_flags: tuple[dict[str, Any], ...] = ()
    qa_passed: Optional[bool] = None

    notes: tuple[Note, ...] = ()

    def evolve(self, **changes: Any) -> "PipelineState":
        """Return a copy with `changes` applied."""
        return dataclasses.replace(self, **changes)

    def with_note(
        self, stage: str, kind: str, count: int, detail: str = ""
    ) -> "PipelineState":
        """Record a deliberate intervention. Zero-count notes are dropped so
        the log only shows what actually happened on this run."""
        if count == 0:
            return self
        return self.evolve(
            notes=self.notes + (Note(stage, kind, count, detail),)
        )
