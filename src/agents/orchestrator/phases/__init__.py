"""Phase modules for the AgentCoordinator.

Each phase class takes a coordinator back-reference and owns the logic
for one stage of the task lifecycle.
"""

from agents.orchestrator.phases.intake import IntakePhase
from agents.orchestrator.phases.planning import PlanningPhase
from agents.orchestrator.phases.execution import ExecutionPhase
from agents.orchestrator.phases.testing import TestingPhase
from agents.orchestrator.phases.review import ReviewPhase
from agents.orchestrator.phases.subtask_lifecycle import SubtaskLifecycle

__all__ = [
    "IntakePhase",
    "PlanningPhase",
    "ExecutionPhase",
    "TestingPhase",
    "ReviewPhase",
    "SubtaskLifecycle",
]
