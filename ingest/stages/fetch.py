"""fetch — pull raw box scores from whichever provider was selected."""

from __future__ import annotations

from ..providers import get_provider
from ..state import PipelineState


def run(state: PipelineState) -> PipelineState:
    provider = get_provider(state.provider_name)
    box = provider.get_box_scores(state.season)
    return state.evolve(
        raw_player_box=box.player_box,
        raw_team_box=box.team_box,
        raw_schedule=box.schedule,
        raw_player_core=box.player_core,
    )
