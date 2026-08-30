"""
Career Pipeline Bounded Context:
Encapsulates pipeline stage boards, state transitions, scoring filters, and preferences.
"""
from ..pipeline_board import (
    BOARD_STAGES,
    STAGE_TAB_LABELS_NORMAL,
    STAGE_TAB_LABELS_POWER,
    applying_view,
    done_view,
    pipeline_board_view,
    pipeline_view,
    vetting_view,
)

__all__ = [
    "BOARD_STAGES",
    "STAGE_TAB_LABELS_NORMAL",
    "STAGE_TAB_LABELS_POWER",
    "pipeline_board_view",
    "pipeline_view",
    "vetting_view",
    "applying_view",
    "done_view",
]
