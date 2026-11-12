"""Event bus and lifecycle event types for the agent loop."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable


# -- Event types --


@dataclass
class Thinking:
    """Emitted when the model produces a thinking/reasoning trace."""

    text: str


@dataclass
class PreToolUse:
    """Emitted before a tool is executed. Approval gate - listeners return True/False.

    ``tool_id`` matches the model's tool call. ``reason`` is the text the model
    wrote alongside the call (why it wants to do this), if any.
    """

    tool_name: str
    tool_params: dict
    tool_id: str = ""
    reason: str = ""


@dataclass
class PostToolUse:
    """Emitted after a tool has executed with its result."""

    tool_name: str
    result: str
    tool_id: str = ""
    tool_params: dict = field(default_factory=dict)


@dataclass
class PlanProposed:
    """Emitted when the agent proposes a plan before acting. Approval gate for the plan."""

    steps: list
    text: str = ""


@dataclass
class Paused:
    """Emitted when the loop reaches a safe point and stops because the user asked to pause."""

    after_step: str = ""


@dataclass
class Resumed:
    """Emitted when a paused loop continues."""

    note: str = ""


@dataclass
class Stop:
    """Emitted when the agent produces a final text response (no more tool calls)."""

    text: str


@dataclass
class SubagentStart:
    """Emitted when a sub-agent begins its task."""

    task: str


@dataclass
class SubagentStop:
    """Emitted when a sub-agent completes its task."""

    task: str
    result: str


# -- Event Bus --


class EventBus:
    """Simple event dispatcher. Listeners are async callables registered by event type."""

    def __init__(self) -> None:
        self._listeners: dict[type, list[Callable]] = defaultdict(list)
        self._approval_handler: Callable | None = None
        self._plan_handler: Callable | None = None

    def on(self, event_type: type, callback: Callable) -> None:
        """Register an async callback for an event type."""
        self._listeners[event_type].append(callback)

    def on_approval(self, callback: Callable) -> None:
        """Register the approval handler for PreToolUse events.

        This is separate from on() so that emit() and emit_approval() don't
        conflict. UI listeners use on(PreToolUse, ...) to render the panel.
        The approval handler uses on_approval(...) to gate execution.
        """
        self._approval_handler = callback

    async def emit(self, event: Any) -> None:
        """Emit an event to all registered listeners."""
        for callback in self._listeners[type(event)]:
            await callback(event)

    async def emit_approval(self, event: PreToolUse) -> bool:
        """Emit to regular listeners, then call the approval handler.

        Returns True if no handler is registered.
        """
        await self.emit(event)
        if self._approval_handler is None:
            return True
        result = await self._approval_handler(event)
        return result is not False

    def on_plan(self, callback: Callable) -> None:
        """Register the handler that approves or rejects a PlanProposed event.

        The callback returns True/False, or a (approved, note) tuple.
        """
        self._plan_handler = callback

    async def emit_plan(self, event: PlanProposed) -> tuple[bool, str]:
        """Emit to regular listeners, then ask the plan handler. Approves if none is registered."""
        await self.emit(event)
        if self._plan_handler is None:
            return True, ""
        result = await self._plan_handler(event)
        if isinstance(result, tuple):
            approved, note = result
            return approved is not False, note or ""
        return result is not False, ""
