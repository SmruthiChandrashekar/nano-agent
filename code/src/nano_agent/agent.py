"""Core agent loop. Manages conversation history, tool dispatch, and event emission."""

import asyncio

from nano_agent.events import (
    EventBus,
    Paused,
    PlanProposed,
    PostToolUse,
    PreToolUse,
    Resumed,
    Stop,
    SubagentStart,
    SubagentStop,
    Thinking,
)
from nano_agent.providers.base import (
    Provider,
    ProviderResponse,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from nano_agent.system_prompt import SYSTEM_PROMPT

MAX_TURNS_MESSAGE = "Maximum agent turns reached ({max_turns})."
STOPPED_MESSAGE = "Stopped by user."
STOPPED_RESULT = "Stopped by user"
PLAN_REJECTED_MESSAGE = "Plan rejected by user."

PLAN_TOOL_NAME = "propose_plan"
PLAN_TOOL_SCHEMA = {
    "name": PLAN_TOOL_NAME,
    "description": (
        "Propose a short plan before changing anything. List each step you intend to take, "
        "the file or command it touches, and why. The user approves the plan before you act."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "What you will do, e.g. 'Delete config_old.json'."},
                        "target": {"type": "string", "description": "The file, folder or command involved."},
                        "why": {"type": "string", "description": "One short reason."},
                    },
                    "required": ["action", "why"],
                },
            },
        },
        "required": ["steps"],
    },
}
PLAN_INSTRUCTION = (
    "\n\nBefore using any other tool, call propose_plan once with your plan. "
    "Look only at what the request says; do not run any other tool in this step. "
    "If the request needs no tools at all (for example a general question), answer it directly in text instead."
)


def _serialize_content(response: ProviderResponse) -> list[dict]:
    """Convert normalized blocks back to message content in the same order."""
    content = []
    for block in response.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            content.append({
                "type": "thinking",
                "thinking": block.thinking,
                "signature": block.signature,
            })
        elif isinstance(block, RedactedThinkingBlock):
            content.append({"type": "redacted_thinking", "data": block.data})
        elif isinstance(block, ToolUseBlock):
            content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return content


class Agent:
    """The agent loop. Contains zero UI or approval logic - all side effects go through the event bus.

    Optional controls for a supervising UI:
    - ``plan_first``: ask the model for a plan and wait for approval before acting.
    - ``request_pause()`` / ``resume(note)``: stop at the next safe point (before a model
      call or a tool) and continue later, optionally with a note for the model.
    - ``request_stop()``: end the request at the next safe point.
    """

    def __init__(
        self,
        provider: Provider,
        event_bus: EventBus,
        tools: dict | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_turns: int = 20,
        plan_first: bool = False,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        self.provider = provider
        self.event_bus = event_bus
        self.tools = tools or {}
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.plan_first = plan_first
        self.history: list[dict] = []
        self._pause_requested = False
        self._resume_event: asyncio.Event | None = None
        self._stop_requested = False
        self._notes: list[str] = []
        self._last_step = ""

    # ------------------------------------------------------------------
    # Controls (called by a UI while run() is in progress)
    # ------------------------------------------------------------------

    def request_pause(self) -> None:
        self._pause_requested = True

    def resume(self, note: str | None = None) -> None:
        if note:
            self.add_note(note)
        self._pause_requested = False
        if self._resume_event is not None:
            self._resume_event.set()

    def request_stop(self) -> None:
        self._stop_requested = True
        self.resume()

    def add_note(self, note: str) -> None:
        """Queue a message from the user; the model sees it on its next call."""
        self._notes.append(note)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    async def _safe_point(self) -> None:
        """Wait here while a pause is requested. Returns immediately otherwise."""
        if not self._pause_requested or self._stop_requested:
            return
        self._resume_event = asyncio.Event()
        await self.event_bus.emit(Paused(after_step=self._last_step))
        await self._resume_event.wait()
        self._resume_event = None
        note = self._notes[-1] if self._notes else ""
        await self.event_bus.emit(Resumed(note=note))

    def _attach_notes(self) -> None:
        """Add queued user notes to the latest user message so the model reads them next."""
        if not self._notes or not self.history:
            return
        last = self.history[-1]
        text = "\n".join(f"Note from the user: {n}" for n in self._notes)
        self._notes.clear()
        if last["role"] != "user":
            self.history.append({"role": "user", "content": text})
            return
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        last["content"].append({"type": "text", "text": text})

    async def _stop(self, text: str) -> str:
        self.history.append({"role": "assistant", "content": text})
        await self.event_bus.emit(Stop(text=text))
        return text

    # ------------------------------------------------------------------
    # Plan step
    # ------------------------------------------------------------------

    async def _propose_plan(self) -> str:
        """Returns "approved", "rejected", or "answered" (the model replied without needing tools)."""
        response = await self.provider.send(
            self.history, [PLAN_TOOL_SCHEMA], self.system_prompt + PLAN_INSTRUCTION
        )
        for block in response.content:
            if isinstance(block, ThinkingBlock):
                await self.event_bus.emit(Thinking(text=block.thinking))
        plan_block = next(
            (b for b in response.content if isinstance(b, ToolUseBlock) and b.name == PLAN_TOOL_NAME),
            None,
        )
        text = "\n".join(b.text for b in response.content if isinstance(b, TextBlock))
        steps = plan_block.input.get("steps", []) if plan_block else []
        self.history.append({"role": "assistant", "content": _serialize_content(response)})
        if not any(isinstance(b, ToolUseBlock) for b in response.content):
            await self.event_bus.emit(Stop(text=text))
            return "answered"

        approved, note = await self.event_bus.emit_plan(PlanProposed(steps=steps, text=text))
        if approved:
            reply = "Plan approved by the user. Carry it out now."
            if note:
                reply += f" Note from the user: {note}"
        else:
            reply = "Plan rejected by the user." + (f" Note: {note}" if note else "")

        tool_results = []
        for block in response.content:
            if isinstance(block, ToolUseBlock):
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": reply})
        self.history.append({"role": "user", "content": tool_results})
        return "approved" if approved else "rejected"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, user_input: str) -> str:
        """Run one turn of the agent loop. Returns the final text response."""
        self._stop_requested = False
        self.history.append({"role": "user", "content": user_input})

        if self.plan_first:
            outcome = await self._propose_plan()
            if outcome == "answered":
                return "\n".join(
                    b["text"] for b in self.history[-1]["content"] if b.get("type") == "text"
                )
            if outcome == "rejected":
                return await self._stop(PLAN_REJECTED_MESSAGE)

        tool_schemas = [tool["schema"] for tool in self.tools.values()]
        if self.plan_first:
            tool_schemas = tool_schemas + [PLAN_TOOL_SCHEMA]

        for _turn in range(self.max_turns):
            await self._safe_point()
            if self._stop_requested:
                return await self._stop(STOPPED_MESSAGE)
            self._attach_notes()

            response: ProviderResponse = await self.provider.send(
                self.history, tool_schemas, self.system_prompt
            )

            thinking_blocks = [
                block for block in response.content if isinstance(block, ThinkingBlock)
            ]
            for block in thinking_blocks:
                await self.event_bus.emit(Thinking(text=block.thinking))

            tool_use_blocks = [b for b in response.content if isinstance(b, ToolUseBlock)]
            text_blocks = [b for b in response.content if isinstance(b, TextBlock)]
            reason = "\n".join(b.text for b in text_blocks).strip()

            if tool_use_blocks:
                # Preserve every response block unchanged and in order.
                assistant_content = _serialize_content(response)
                self.history.append({"role": "assistant", "content": assistant_content})

                # Separate spawn_agent calls from regular tool calls
                spawn_blocks = [b for b in tool_use_blocks if b.name == "spawn_agent"]
                regular_blocks = [b for b in tool_use_blocks if b.name != "spawn_agent"]

                tool_results = []

                # Process regular tool calls sequentially
                for block in regular_blocks:
                    await self._safe_point()
                    if self._stop_requested:
                        result = STOPPED_RESULT
                    elif block.name == PLAN_TOOL_NAME:
                        result = "Plan already approved. Continue with the task."
                    else:
                        approved = await self.event_bus.emit_approval(
                            PreToolUse(
                                tool_name=block.name,
                                tool_params=block.input,
                                tool_id=block.id,
                                reason=reason,
                            )
                        )

                        if self._stop_requested:
                            result = STOPPED_RESULT
                        elif approved and block.name in self.tools:
                            tool_fn = self.tools[block.name]["function"]
                            try:
                                result = await tool_fn(**block.input)
                            except Exception as e:
                                result = f"Error: {e}"
                        elif not approved:
                            result = "Tool call denied by user"
                        else:
                            result = f"Unknown tool: {block.name}"

                    self._last_step = block.id
                    await self.event_bus.emit(
                        PostToolUse(
                            tool_name=block.name,
                            result=result,
                            tool_id=block.id,
                            tool_params=block.input,
                        )
                    )

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                # Approve spawn_agent calls before they run concurrently
                if spawn_blocks:
                    approved_spawn_blocks = []
                    for block in spawn_blocks:
                        approved = await self.event_bus.emit_approval(
                            PreToolUse(
                                tool_name=block.name,
                                tool_params=block.input,
                                tool_id=block.id,
                                reason=reason,
                            )
                        )
                        if approved:
                            approved_spawn_blocks.append(block)
                            continue

                        result = "Tool call denied by user"
                        await self.event_bus.emit(
                            PostToolUse(tool_name=block.name, result=result, tool_id=block.id, tool_params=block.input)
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                    spawn_results = await self._run_subagents(approved_spawn_blocks)
                    for result in spawn_results:
                        await self.event_bus.emit(
                            PostToolUse(
                                tool_name="spawn_agent",
                                result=result["content"],
                                tool_id=result["tool_use_id"],
                            )
                        )
                    tool_results.extend(spawn_results)

                self.history.append({"role": "user", "content": tool_results})

            else:
                # Text-only response - we're done
                text = "\n".join(b.text for b in text_blocks)
                assistant_content = _serialize_content(response)
                self.history.append({"role": "assistant", "content": assistant_content})
                await self.event_bus.emit(Stop(text=text))
                return text

        if self._stop_requested:
            return await self._stop(STOPPED_MESSAGE)
        return await self._stop(MAX_TURNS_MESSAGE.format(max_turns=self.max_turns))

    async def _run_subagents(self, spawn_blocks: list[ToolUseBlock]) -> list[dict]:
        """Run sub-agents concurrently and return tool result dicts."""
        # Build sub-agent tools: same as parent but without spawn_agent (no recursion)
        sub_tools = {k: v for k, v in self.tools.items() if k != "spawn_agent"}

        async def run_one(block: ToolUseBlock) -> dict:
            task = block.input.get("task", "")

            # Sub-agent gets its own event bus with auto-approve
            sub_bus = EventBus()

            async def auto_approve(_event: PreToolUse) -> bool:
                return True

            sub_bus.on_approval(auto_approve)

            sub_agent = Agent(
                provider=self.provider,
                event_bus=sub_bus,
                tools=sub_tools,
                system_prompt=self.system_prompt,
                max_turns=self.max_turns,
            )

            await self.event_bus.emit(SubagentStart(task=task))
            try:
                result = await sub_agent.run(task)
            except Exception as e:
                result = f"Sub-agent error: {e}"
            await self.event_bus.emit(SubagentStop(task=task, result=result))

            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            }

        results = await asyncio.gather(*[run_one(b) for b in spawn_blocks])
        return list(results)
