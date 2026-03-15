from agents.agents.base import BaseSpecialistAgent
from agents.utils.json_helpers import parse_llm_json


class ReviewerAgent(BaseSpecialistAgent):
    role = "reviewer"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a senior code reviewer. Review code for:\n"
            "1. Correctness — does it do what it's supposed to?\n"
            "2. Security — any injection, auth bypass, data exposure issues?\n"
            "3. Performance — obvious N+1 queries, unbounded loops, missing indexes?\n"
            "4. Maintainability — clear naming, reasonable complexity?\n"
            "5. Edge cases — null/empty inputs, error paths?\n\n"
            "Output a JSON object with:\n"
            '- "approved": boolean\n'
            '- "issues": list of {severity, description, suggestion}\n'
            '- "summary": brief overall assessment\n'
            '- "needs_human_review": boolean (true only if you are uncertain)'
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        parts = [f"Review context: {task}\n"]
        if previous_results:
            for r in previous_results:
                parts.append(f"--- Code from [{r.get('agent_role', '?')}] ---")
                output = r.get("output_result", {})
                content = (
                    output.get("content", str(output))
                    if isinstance(output, dict) else str(output)
                )
                parts.append(content[:4000])
        return "\n".join(parts)

    def parse_output(self, content: str) -> dict:
        result = parse_llm_json(content)
        if result is not None:
            return result
        return {"approved": True, "issues": [], "summary": content, "needs_human_review": True}
