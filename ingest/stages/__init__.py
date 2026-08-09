"""
Pipeline stages, one module each (CLAUDE.md).

    fetch -> validate -> normalize -> transactions -> aggregate -> qa -> publish

Each is a plain function `(PipelineState) -> PipelineState`. No stage
imports another; the orchestrator composes them. That means any stage can
be tested in isolation by handing it a state, and reordering the pipeline
is an edit in one place.
"""
