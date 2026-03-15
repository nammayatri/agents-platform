"""Intake agent — AI interview to gather requirements.

Decides whether the task description is clear enough to proceed to
planning or whether follow-up questions should be asked of the user.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agents.orchestrator.agent_result import AgentResult
from agents.orchestrator.agents._base import BaseAgent
from agents.schemas.agent import LLMMessage
from agents.utils.json_helpers import parse_llm_json

if TYPE_CHECKING:
    from agents.orchestrator.run_context import RunContext

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


class IntakeAgent(BaseAgent):
    """Gather requirements via AI interview."""

    role = "intake"

    async def run(self, job: dict, workspace: str | None, ctx: RunContext) -> AgentResult:
        """Run the intake interview.

        This agent runs during the 'intake' phase — the job dict is actually
        the todo_items row, not a sub_tasks row.
        """
        todo = await ctx.load_todo()
        provider = await ctx.provider_registry.resolve_for_todo(ctx.todo_id)

        # Build context
        project = await ctx.load_project(str(todo["project_id"]))
        context = {}
        if project:
            context["project_name"] = project.get("name", "")
            context["repo_url"] = project.get("repo_url", "")

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

        # Check for existing chat messages (user may have already answered)
        if ctx.chat_session_id:
            chat_rows = await ctx.db.fetch(
                "SELECT role, content FROM project_chat_messages "
                "WHERE session_id = $1 AND role IN ('user', 'assistant') "
                "ORDER BY created_at",
                ctx.chat_session_id,
            )
            for msg in chat_rows:
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
        await ctx.track_tokens(response)

        result = extract_submit_result(
            response.tool_calls, response.content, messages=messages,
        )

        if result is None:
            # Retry once with correction prompt
            logger.warning("Intake: failed to parse result, retrying")
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
            retry = await provider.send_message(messages, temperature=0.1)
            await ctx.track_tokens(retry)
            result = parse_llm_json(retry.content)

        if result is None:
            # Default to ready
            logger.warning("Intake: parse failed after retry, defaulting to ready=true")
            result = {
                "ready": True,
                "requirements": todo["title"],
                "approach": "Determined from task description and project context",
            }

        return AgentResult(output=result)
