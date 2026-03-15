import json

from agents.agents.base import BaseSpecialistAgent
from agents.utils.json_helpers import parse_llm_json


class PlannerAgent(BaseSpecialistAgent):
    role = "planner"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a task planner and decomposer. Your job is to break down a task "
            "into concrete, actionable sub-tasks that can be executed by specialist agents.\n\n"
            "Available agent roles:\n"
            "- coder: Writes code, implements features, fixes bugs\n"
            "- tester: Writes tests, validates implementations\n"
            "- reviewer: Reviews code quality, security, performance\n"
            "- pr_creator: Creates pull request descriptions\n"
            "- report_writer: Creates reports and documentation\n\n"
            "Design for maximum parallelism — tasks with the same execution_order "
            "run simultaneously.\n\n"
            "Output a JSON object with:\n"
            '- "summary": plan overview\n'
            '- "sub_tasks": list of {title, description, agent_role, execution_order, depends_on}\n'
            '- "estimated_tokens": rough token estimate'
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        return (
            f"Task: {task}\n\n"
            f"Project: {context.get('project_name', 'Unknown')}\n"
            f"Repository: {context.get('repo_url', 'N/A')}\n"
            f"Requirements: {json.dumps(context.get('requirements', {}), default=str)}\n"
        )

    def parse_output(self, content: str) -> dict:
        result = parse_llm_json(content)
        if result is not None:
            return result
        return {"summary": content, "sub_tasks": [], "estimated_tokens": 0}
