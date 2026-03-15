"""Event handler registry — the single place to see what happens for each role.

COMPLETION_HANDLERS: called after a subtask completes (review-merge loop).
EXECUTION_HANDLERS: procedural handlers for non-LLM roles.
"""

from agents.orchestrator.handlers.on_coder_complete import handle_coder_completion
from agents.orchestrator.handlers.on_reviewer_complete import handle_reviewer_completion
from agents.orchestrator.handlers.on_tester_complete import handle_tester_completion
from agents.orchestrator.handlers.exec_pr_creator import execute_pr_creator
from agents.orchestrator.handlers.exec_merge_agent import execute_merge_agent
from agents.orchestrator.handlers.exec_merge_observer import execute_merge_observer
from agents.orchestrator.handlers.exec_build_watcher import execute_build_watcher
from agents.orchestrator.handlers.exec_release_deployer import execute_release_deployer

# ── What happens when a subtask of role X completes ──────────────
COMPLETION_HANDLERS = {
    "coder":    handle_coder_completion,      # → create reviewer
    "reviewer": handle_reviewer_completion,   # → create PR (approved) or fix subtasks
    "tester":   handle_tester_completion,     # → create fix subtasks or pass
}

# ── Procedural execution handlers (non-LLM roles) ───────────────
EXECUTION_HANDLERS = {
    "pr_creator":            execute_pr_creator,
    "merge_agent":           execute_merge_agent,
    "merge_observer":        execute_merge_observer,
    "release_build_watcher": execute_build_watcher,
    "release_deployer":      execute_release_deployer,
}
