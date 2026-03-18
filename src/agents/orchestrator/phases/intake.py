"""Intake phase — AI interview to gather requirements.

Decides whether the task description is clear enough to proceed to
planning or whether follow-up questions should be asked of the user.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agents.schemas.agent import LLMMessage
from agents.utils.json_helpers import parse_llm_json

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)

INTAKE_SYSTEM_PROMPT = """\
You are a task intake agent. Decide if the task is clear enough to start.

Default to ready=true. Only set ready=false for genuinely ambiguous tasks with \
contradictory requirements or multiple fundamentally different interpretations.

Output JSON only:
{"ready": true, "requirements": "concise summary", "approach": "one-line approach", "auto_answerable": []}
or
{"ready": false, "questions": ["1-2 critical questions only"]}
"""


class IntakePhase:
    """Gather all requirements upfront via AI interview."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    async def run(self, todo: dict, provider: AIProvider) -> None:
        """Gather all requirements upfront via AI interview."""
        coord = self._coord
        context = await coord._build_context(todo)

        messages = [
            LLMMessage(role="system", content=INTAKE_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Task: {todo['title']}\n"
                    f"Description: {todo['description'] or 'No description provided'}\n"
                    f"Type: {todo['task_type']}\n"
                    f"Project context: {json.dumps(context, default=str)}"
                ),
            ),
        ]

        # Check if there are existing chat messages (user may have already answered questions)
        chat_history = await coord._load_chat_history()
        if chat_history:
            for msg in chat_history:
                messages.append(LLMMessage(role=msg["role"], content=msg["content"]))

        # Build submit_result tool for structured intake output
        from agents.orchestrator.structured_output import build_submit_tool, extract_submit_result

        intake_schema = {
            "type": "object",
            "properties": {
                "ready": {"type": "boolean", "description": "True if task is clear enough to start"},
                "requirements": {"type": "string", "description": "Concise summary of requirements"},
                "approach": {"type": "string", "description": "One-line approach"},
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Questions to ask (only if ready=false)",
                },
            },
            "required": ["ready"],
        }
        submit_tool = build_submit_tool(intake_schema, "intake")

        response = await provider.send_message(
            messages,
            temperature=0.2,
            tools=[submit_tool],
            tool_choice={"name": "submit_result"},
        )
        await coord._track_tokens(response)

        # Extract from tool call first, then text fallback
        result = extract_submit_result(
            response.tool_calls, response.content, messages=messages,
        )

        if result is None:
            # Retry once with a correction prompt before defaulting
            logger.warning("Intake: failed to parse result, retrying with correction prompt")
            messages.append(LLMMessage(role="assistant", content=response.content))
            messages.append(LLMMessage(
                role="user",
                content=(
                    "Your response was not valid JSON. Please respond with ONLY a JSON object "
                    'with keys: "ready" (boolean), "requirements" (string or object), '
                    '"approach" (string). If you need to ask questions, include '
                    '"questions" (list of strings) and set "ready" to false.'
                ),
            ))
            retry_response = await provider.send_message(messages, temperature=0.1)
            await coord._track_tokens(retry_response)
            result = parse_llm_json(retry_response.content)

        if result is None:
            # If retry also failed, default to ready with the task title as requirements
            logger.warning("Intake: parse failed after retry, defaulting to ready=true")
            result = {
                "ready": True,
                "requirements": todo["title"],
                "approach": "Determined from task description and project context",
            }

        if result.get("ready", False):
            # Enough info gathered — store intake data and move to planning
            intake_data = {
                "requirements": result.get("requirements", {}),
                "approach": result.get("approach", ""),
                "auto_answers": result.get("auto_answerable", []),
            }
            await coord.db.execute(
                "UPDATE todo_items SET intake_data = $2 WHERE id = $1",
                coord.todo_id,
                intake_data,
            )
            await coord._post_system_message(
                f"**Intake complete.** Moving to planning.\n\n"
                f"**Requirements:** {json.dumps(result.get('requirements', {}), indent=2)}\n\n"
                f"**Approach:** {result.get('approach', 'Auto-determined')}"
            )
            await coord._transition_todo("planning", sub_state="decomposing")
            # Immediately continue to planning
            todo = await coord._load_todo()
            await coord._planning.run(todo, provider)
        else:
            # Need to ask human questions — mark as awaiting_response so the
            # orchestrator loop does NOT re-dispatch until the user replies.
            questions = result.get("questions", [])
            if questions:
                q_text = "\n".join(f"- {q}" for q in questions)
                await coord._post_system_message(
                    f"**Quick question before proceeding:**\n\n{q_text}\n\n"
                    "Reply in the chat, or the task will auto-proceed with reasonable defaults."
                )
                await coord.db.execute(
                    "UPDATE todo_items SET sub_state = 'awaiting_response', updated_at = NOW() WHERE id = $1",
                    coord.todo_id,
                )
