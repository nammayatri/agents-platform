
from agents.agents.base import BaseSpecialistAgent


class PRAgent(BaseSpecialistAgent):
    role = "pr_creator"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a PR description writer. Create clear, detailed pull request descriptions.\n\n"
            "Include:\n"
            "- Summary of changes (2-3 sentences)\n"
            "- Detailed list of changes made\n"
            "- Testing done\n"
            "- Any breaking changes or migration notes\n"
            "- Screenshots/examples if applicable\n\n"
            "Use markdown formatting. Be concise but thorough."
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        parts = [f"Create a PR description for: {task}\n"]
        if context.get("repo_url"):
            parts.append(f"Repository: {context['repo_url']}")
            parts.append(f"Branch: {context.get('default_branch', 'main')}")

        if previous_results:
            parts.append("\nWork completed:")
            for r in previous_results:
                output = r.get("output_result", {})
                content = (
                    output.get("content", str(output))
                    if isinstance(output, dict) else str(output)
                )
                parts.append(f"\n--- [{r.get('agent_role', '?')}] {r.get('title', '?')} ---")
                parts.append(content[:3000])

        return "\n".join(parts)
