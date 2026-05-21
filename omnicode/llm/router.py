from typing import List, Dict, Optional, Any
from enum import Enum
import logging
from .base import BaseLLMProvider, LLMResponse, LLMMessage
from .providers.litellm_provider import LiteLLMProvider
from ..config import get_settings

logger = logging.getLogger(__name__)

class RoutingStrategy(str, Enum):
    COST_OPTIMIZED = "cost_optimized"
    QUALITY_FIRST = "quality_first"
    FASTEST = "fastest"

class LLMRouter:
    """
    Intelligent router for LLM requests.
    Handles fallback chains, retry logic, and routing strategies.
    """
    def __init__(self):
        self.settings = get_settings()
        self.providers: Dict[str, BaseLLMProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize available providers based on settings"""
        # We use litellm for all of them, but we set up the specific models
        
        # Define our fallback chains based on quality/cost
        self.quality_chain = []
        self.cost_chain = []
        
        if self.settings.ANTHROPIC_API_KEY:
            self.providers["claude"] = LiteLLMProvider("claude-3-opus-20240229", self.settings.ANTHROPIC_API_KEY)
            self.providers["claude_fast"] = LiteLLMProvider("claude-3-haiku-20240307", self.settings.ANTHROPIC_API_KEY)
            self.quality_chain.append("claude")
            self.cost_chain.append("claude_fast")
            
        if self.settings.OPENAI_API_KEY:
            self.providers["openai"] = LiteLLMProvider("gpt-4o", self.settings.OPENAI_API_KEY)
            self.providers["openai_fast"] = LiteLLMProvider("gpt-4o-mini", self.settings.OPENAI_API_KEY)
            self.quality_chain.append("openai")
            self.cost_chain.append("openai_fast")
            
        if self.settings.GEMINI_API_KEY:
            self.providers["gemini"] = LiteLLMProvider("gemini/gemini-1.5-pro", self.settings.GEMINI_API_KEY)
            self.providers["gemini_fast"] = LiteLLMProvider("gemini/gemini-1.5-flash", self.settings.GEMINI_API_KEY)
            self.quality_chain.append("gemini")
            self.cost_chain.insert(0, "gemini_fast") # Gemini flash is often cheapest/fastest
            
        if self.settings.DEEPSEEK_API_KEY:
            self.providers["deepseek"] = LiteLLMProvider("deepseek/deepseek-coder", self.settings.DEEPSEEK_API_KEY)
            self.cost_chain.insert(0, "deepseek")
            
        # Default fallback if nothing is configured
        if not self.providers:
            default_model = self.settings.DEFAULT_LLM_MODEL
            # If no API key is set, litellm will try to find it in the environment
            self.providers["default"] = LiteLLMProvider(default_model)
            self.quality_chain.append("default")
            self.cost_chain.append("default")
            
        logger.info(f"Initialized LLM Router with {len(self.providers)} providers.")

    def _get_provider_chain(self, strategy: RoutingStrategy) -> List[str]:
        if strategy == RoutingStrategy.QUALITY_FIRST:
            return self.quality_chain + self.cost_chain
        elif strategy == RoutingStrategy.COST_OPTIMIZED:
            return self.cost_chain + self.quality_chain
        else: # FASTEST
            return self.cost_chain

    async def complete(
        self, 
        messages: List[LLMMessage], 
        strategy: RoutingStrategy = RoutingStrategy.QUALITY_FIRST,
        **kwargs
    ) -> LLMResponse:
        """
        Execute a completion request with fallback routing.
        """
        chain = self._get_provider_chain(strategy)
        
        if not chain:
            raise ValueError("No LLM providers available in the routing chain.")
            
        last_error = None
        
        for provider_name in chain:
            if provider_name not in self.providers:
                continue
                
            provider = self.providers[provider_name]
            try:
                logger.info(f"Routing request to {provider_name} (Model: {provider.model_name})")
                response = await provider.complete(messages, **kwargs)
                return response
                
            except Exception as e:
                logger.warning(f"Provider {provider_name} failed: {str(e)}. Trying next in chain.")
                last_error = e
                
        logger.error(f"All providers in chain failed. Last error: {str(last_error)}")
        raise RuntimeError(f"LLM routing failed. Last error: {str(last_error)}")
