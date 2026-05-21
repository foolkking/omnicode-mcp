from typing import AsyncIterator, List, Optional, Dict, Any
import litellm
import tiktoken
import logging
from ..base import BaseLLMProvider, LLMResponse, LLMMessage, Role

logger = logging.getLogger(__name__)

# Configure LiteLLM
litellm.drop_params = True # Silently drop unsupported params
litellm.success_callback = [] # Can add custom tracking here

class LiteLLMProvider(BaseLLMProvider):
    """
    Unified LLM provider using LiteLLM to support 100+ models.
    Model name format should follow litellm conventions (e.g., 'gpt-4o', 'claude-3-opus-20240229', 'gemini/gemini-1.5-flash').
    """
    def __init__(self, model_name: str, api_key: Optional[str] = None):
        super().__init__(model_name, api_key)
        
        # Configure litellm with specific API key if provided
        self._completion_kwargs = {}
        if api_key:
            self._completion_kwargs["api_key"] = api_key

    def _convert_messages(self, messages: List[LLMMessage]) -> List[Dict[str, str]]:
        return [{"role": msg.role.value, "content": msg.content} for msg in messages]

    async def complete(
        self, 
        messages: List[LLMMessage], 
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        
        litellm_messages = self._convert_messages(messages)
        
        try:
            response = await litellm.acompletion(
                model=self.model_name,
                messages=litellm_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **self._completion_kwargs,
                **kwargs
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
                cost=cost
            )
            
        except Exception as e:
            logger.error(f"LiteLLM completion error with {self.model_name}: {str(e)}")
            raise

    async def stream(
        self, 
        messages: List[LLMMessage], 
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
        **kwargs
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
                **kwargs
            )
            
            async for chunk in response_stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'content') and delta.content:
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
            return model_info.get("max_input_tokens", 8192) # Default fallback
        except Exception:
            return 8192
