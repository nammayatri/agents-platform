
from agents.agents.base import BaseSpecialistAgent


class ReportAgent(BaseSpecialistAgent):
    role = "report_writer"

    def build_system_prompt(self, context: dict) -> str:
        return (
            "You are a technical writer. Create clear, well-structured reports.\n\n"
            "Include:\n"
            "- Executive summary\n"
            "- Key findings or results\n"
            "- Methodology (if applicable)\n"
            "- Detailed analysis\n"
            "- Recommendations or next steps\n\n"
            "Use markdown formatting with headers, lists, and tables where appropriate."
        )

    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        parts = [f"Write a report for: {task}\n"]
        if previous_results:
            parts.append("Data and results to include:")
            for r in previous_results:
                output = r.get("output_result", {})
                content = (
                    output.get("content", str(output))
                    if isinstance(output, dict) else str(output)
                )
                parts.append(f"\n--- [{r.get('agent_role', '?')}] {r.get('title', '?')} ---")
                parts.append(content[:3000])
        return "\n".join(parts)
