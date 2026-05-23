import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import litellm
import tiktoken

from ..base import BaseLLMProvider, LLMMessage, LLMResponse

logger = logging.getLogger(__name__)

# Configure LiteLLM
litellm.drop_params = True  # Silently drop unsupported params
litellm.success_callback = []  # Can add custom tracking here


class LiteLLMProvider(BaseLLMProvider):
    """
    Unified LLM provider using LiteLLM to support 100+ models.

    Model name format follows litellm conventions, e.g.:
      * "gpt-4o", "gpt-4o-mini"
      * "claude-3-opus-20240229"
      * "gemini/gemini-1.5-flash"
      * "deepseek/deepseek-coder"
      * "ollama/llama3"        (requires api_base="http://localhost:11434")
      * "azure/<deployment>"   (requires api_base + AZURE_API_VERSION header)
      * "openai/<custom>"      with api_base for OpenAI-compatible proxies
    """

    def __init__(
        self,
        model_name: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(model_name, api_key)

        # Configure LiteLLM with the per-instance overrides
        self._completion_kwargs: Dict[str, Any] = {}
        if api_key:
            self._completion_kwargs["api_key"] = api_key
        if api_base:
            self._completion_kwargs["api_base"] = api_base
        if extra_headers:
            # LiteLLM honours `extra_headers` for OpenAI/Azure/Anthropic flows
            self._completion_kwargs["extra_headers"] = dict(extra_headers)

        self.api_base = api_base
        self.extra_headers = dict(extra_headers) if extra_headers else {}

    def _convert_messages(self, messages: List[LLMMessage]) -> List[Dict[str, str]]:
        return [{"role": msg.role.value, "content": msg.content} for msg in messages]

    async def complete(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:

        litellm_messages = self._convert_messages(messages)

        try:
            response = await litellm.acompletion(
                model=self.model_name,
                messages=litellm_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **self._completion_kwargs,
                **kwargs,
            )

            content = response.choices[0].message.content
            usage = response.usage

            # Calculate cost (LiteLLM has built-in cost calculation)
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:
                cost = 0.0

            return LLMResponse(
                content=content or "",
                model_name=response.model,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
                cost=cost,
            )

        except Exception as e:
            logger.error(f"LiteLLM completion error with {self.model_name}: {str(e)}")
            raise

    async def stream(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[str]:

        litellm_messages = self._convert_messages(messages)

        try:
            response_stream = await litellm.acompletion(
                model=self.model_name,
                messages=litellm_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **self._completion_kwargs,
                **kwargs,
            )

            async for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, "content") and delta.content:
                        yield delta.content

        except Exception as e:
            logger.error(f"LiteLLM streaming error with {self.model_name}: {str(e)}")
            raise

    def count_tokens(self, text: str) -> int:
        """Count tokens using litellm's built-in counter (defaults to tiktoken)"""
        try:
            return litellm.token_counter(model=self.model_name, text=text)
        except Exception:
            # Fallback to tiktoken cl100k_base
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))

    def get_context_window(self) -> int:
        """Get max context window for the model from litellm model info"""
        try:
            model_info = litellm.get_model_info(self.model_name)
            return model_info.get("max_input_tokens", 8192)  # Default fallback
        except Exception:
            return 8192
