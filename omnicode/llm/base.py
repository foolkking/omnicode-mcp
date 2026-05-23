from abc import ABC, abstractmethod
from enum import Enum
from typing import AsyncIterator, List, Optional

from pydantic import BaseModel


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class LLMMessage(BaseModel):
    role: Role
    content: str

class LLMResponse(BaseModel):
    content: str
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0

class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers."""

    def __init__(self, model_name: str, api_key: Optional[str] = None):
        self.model_name = model_name
        self.api_key = api_key

    @abstractmethod
    async def complete(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        """Get a completion from the model."""
        pass

    @abstractmethod
    async def stream(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncIterator[str]:
        """Stream a completion from the model."""
        pass

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Count tokens for the specific model."""
        pass

    @abstractmethod
    def get_context_window(self) -> int:
        """Get maximum context window for the model."""
        pass
