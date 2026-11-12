"""YAML-based agent configuration."""

from dataclasses import dataclass
from pathlib import Path

import yaml

PROVIDERS = ("gemini", "anthropic")
DEFAULT_PROVIDER = "gemini"
DEFAULT_MODELS = {
    "gemini": "gemini-3.7-flash",
    "anthropic": "claude-sonnet-4-6",
}


@dataclass
class AgentConfig:
    """Top-level agent configuration.

    ``model`` left empty means "the default model for this provider".
    """

    provider: str = DEFAULT_PROVIDER
    model: str = ""
    max_tokens: int = 16000
    max_turns: int = 20
    thinking_mode: str = "adaptive"
    thinking_budget_tokens: int | None = None
    skip_approval: bool = False

    def __post_init__(self) -> None:
        if not self.model and self.provider in DEFAULT_MODELS:
            self.model = DEFAULT_MODELS[self.provider]

    def validate(self) -> None:
        """Reject settings that the agent or the model API cannot use."""
        if self.provider not in PROVIDERS:
            raise ValueError("provider must be one of: " + ", ".join(PROVIDERS))
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if self.thinking_mode not in {"adaptive", "enabled", "disabled"}:
            raise ValueError(
                "thinking_mode must be one of: adaptive, enabled, disabled"
            )
        if self.thinking_mode == "enabled":
            if self.thinking_budget_tokens is None:
                raise ValueError(
                    "thinking_budget_tokens is required when thinking_mode is enabled"
                )
            if self.provider == "anthropic" and self.thinking_budget_tokens < 1024:
                raise ValueError("thinking_budget_tokens must be at least 1024")
            if self.thinking_budget_tokens < 0:
                raise ValueError("thinking_budget_tokens must not be negative")
            if self.thinking_budget_tokens >= self.max_tokens:
                raise ValueError("thinking_budget_tokens must be less than max_tokens")
        elif self.thinking_budget_tokens is not None:
            raise ValueError(
                "thinking_budget_tokens is only valid when thinking_mode is enabled"
            )


DEFAULT_CONFIG_PATHS = [
    Path("nano-agent.yml"),
    Path("nano-agent.yaml"),
    Path(".nano-agent.yml"),
    Path(".nano-agent.yaml"),
]


def load_config(path: str | Path | None = None) -> AgentConfig:
    """Load config from a YAML file.

    If no path is given, searches for default config files in the current directory.
    Returns a default AgentConfig if no config file is found.
    """
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        return _parse_config(config_path)

    # Search default locations
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return _parse_config(candidate)

    return AgentConfig()


def _parse_config(path: Path) -> AgentConfig:
    """Parse a YAML config file into an AgentConfig."""
    raw = yaml.safe_load(path.read_text()) or {}

    config = AgentConfig(
        provider=raw.get("provider", DEFAULT_PROVIDER),
        model=raw.get("model") or "",
        max_tokens=raw.get("max_tokens", 16000),
        max_turns=raw.get("max_turns", 20),
        thinking_mode=raw.get("thinking_mode", "adaptive"),
        thinking_budget_tokens=raw.get("thinking_budget_tokens"),
        skip_approval=raw.get("skip_approval", False),
    )
    config.validate()
    return config
