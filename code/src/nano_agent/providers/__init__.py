from .anthropic import AnthropicProvider
from .base import (
    Provider,
    ProviderError,
    ProviderResponse,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from .gemini import DEFAULT_GEMINI_MODEL, GeminiProvider


def make_provider(config) -> Provider:
    """Build the provider named in an AgentConfig."""
    kwargs = dict(
        model=config.model,
        max_tokens=config.max_tokens,
        thinking_mode=config.thinking_mode,
        thinking_budget_tokens=config.thinking_budget_tokens,
    )
    if config.provider == "gemini":
        return GeminiProvider(**kwargs)
    return AnthropicProvider(**kwargs)


__all__ = [
    "AnthropicProvider",
    "DEFAULT_GEMINI_MODEL",
    "GeminiProvider",
    "Provider",
    "ProviderError",
    "ProviderResponse",
    "RedactedThinkingBlock",
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "make_provider",
]
