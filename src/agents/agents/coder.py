import json

from agents.agents.base import BaseSpecialistAgent


class CoderAgent(BaseSpecialistAgent):
    role = "coder"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a senior software engineer. Write clean, production-quality code.\n\n"
            "Guidelines:\n"
            "- Include all necessary imports\n"
            "- Add error handling for external calls and user input\n"
            "- Follow the project's existing patterns and conventions\n"
            "- Only add comments for non-obvious logic\n"
            "- Output complete, runnable code (not snippets)\n\n"
            "Structure your output as:\n"
            "1. Brief explanation of approach\n"
            "2. Complete code with file paths\n"
            "3. Any setup or migration steps needed"
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        parts = [f"Task: {task}\n"]

        if context.get("repo_url"):
            parts.append(f"Repository: {context['repo_url']}")
        if context.get("requirements"):
            parts.append(f"Requirements: {json.dumps(context['requirements'], default=str)}")

        if previous_results:
            parts.append("\nPrevious work completed:")
            for r in previous_results:
                parts.append(f"- {r.get('title', '?')}: {str(r.get('output_result', ''))[:500]}")

        return "\n".join(parts)
