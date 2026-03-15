
from agents.agents.base import BaseSpecialistAgent


class TesterAgent(BaseSpecialistAgent):
    role = "tester"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a test engineer. Write comprehensive tests.\n\n"
            "Guidelines:\n"
            "- Cover happy paths, edge cases, and error conditions\n"
            "- Use appropriate testing framework for the language\n"
            "- Include setup/teardown when needed\n"
            "- Mock external dependencies\n"
            "- Write clear test names that describe the scenario\n"
            "- Output complete, runnable test code"
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        parts = [f"Write tests for: {task}\n"]
        if previous_results:
            parts.append("Code to test:")
            for r in previous_results:
                if r.get("agent_role") == "coder":
                    output = r.get("output_result", {})
                    content = (
                        output.get("content", str(output))
                        if isinstance(output, dict) else str(output)
                    )
                    parts.append(content[:4000])
        return "\n".join(parts)
