"""Base specialist agent interface.

All specialist agents inherit from this. The coordinator invokes agents
via the provider layer — agents themselves are stateless prompt+logic wrappers.
"""

from abc import ABC, abstractmethod

from agents.providers.base import AIProvider
from agents.schemas.agent import AgentResult, LLMMessage


class BaseSpecialistAgent(ABC):
    """Base class for all specialist agents."""

    role: str = "generic"

    def __init__(self, provider: AIProvider):
        self.provider = provider

    @abstractmethod
    def build_system_prompt(self, context: dict) -> str:
        """Build the system prompt for this agent."""
        ...

    @abstractmethod
    def build_user_prompt(self, task: str, context: dict, previous_results: list[dict]) -> str:
        """Build the user prompt with task details and context."""
        ...

    def parse_output(self, content: str) -> dict:
        """Parse the LLM output into structured data. Override for custom parsing."""
        return {"content": content}

    async def run(
        self,
        task: str,
        context: dict,
        previous_results: list[dict] | None = None,
    ) -> AgentResult:
        """Execute this agent's task."""
        previous_results = previous_results or []

        system_prompt = self.build_system_prompt(context)
        user_prompt = self.build_user_prompt(task, context, previous_results)

        response = await self.provider.send_message(
            [
                LLMMessage(role="system", content=system_prompt),
                LLMMessage(role="user", content=user_prompt),
            ],
            temperature=0.1,
        )

        output = self.parse_output(response.content)

        return AgentResult(
            success=True,
            output=output,
            tokens_used=response.tokens_input + response.tokens_output,
            cost_usd=response.cost_usd,
        )
