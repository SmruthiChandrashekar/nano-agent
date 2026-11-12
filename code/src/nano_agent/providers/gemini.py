"""Google Gemini provider (google-genai SDK).

The agent keeps its history in the Anthropic-style shape used everywhere else in
nano-agent (``tool_use`` / ``tool_result`` blocks). This provider translates that
history into Gemini ``Content`` objects on every call, and translates Gemini's reply
back into the shared blocks.

Gemini 3 models attach *thought signatures* to their parts and reject a follow-up
request whose function calls lost them. So every reply Gemini sends is kept exactly
as received, and replayed unchanged when that turn appears in the history again.
"""

from __future__ import annotations

import os
import uuid

from google import genai
from google.genai import types

from .base import (
    Provider,
    ProviderError,
    ProviderResponse,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"


class GeminiProvider(Provider):
    """Gemini API implementation with optional thinking settings."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_MODEL,
        max_tokens: int = 16000,
        thinking_mode: str = "adaptive",
        thinking_budget_tokens: int | None = None,
        api_key: str | None = None,
        client: object | None = None,
    ) -> None:
        if thinking_mode not in {"adaptive", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be one of: adaptive, enabled, disabled")
        if thinking_mode == "enabled" and thinking_budget_tokens is None:
            raise ValueError("thinking_budget_tokens is required when thinking_mode is enabled")
        if thinking_mode != "enabled" and thinking_budget_tokens is not None:
            raise ValueError("thinking_budget_tokens is only valid when thinking_mode is enabled")

        self.model = model
        self.max_tokens = max_tokens
        self.thinking_mode = thinking_mode
        self.thinking_budget_tokens = thinking_budget_tokens
        if client is not None:
            self.client = client
        else:
            key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self.client = genai.Client(api_key=key) if key else genai.Client()
        # Replies exactly as Gemini sent them, keyed by our tool-call ids or reply text.
        self._raw_turns: dict[str, types.Content] = {}
        # Our tool-call id -> the id Gemini used (Gemini may leave ids empty).
        self._call_ids: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Saving a chat (so it can continue after a restart, thought signatures included)
    # ------------------------------------------------------------------

    def export_state(self) -> dict:
        return {
            "raw_turns": {k: c.model_dump(mode="json", exclude_none=True) for k, c in self._raw_turns.items()},
            "call_ids": self._call_ids,
        }

    def import_state(self, state: dict | None) -> None:
        if not state:
            return
        for key, data in (state.get("raw_turns") or {}).items():
            try:
                self._raw_turns[key] = types.Content.model_validate(data)
            except Exception:
                continue
        self._call_ids.update(state.get("call_ids") or {})

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------

    def _config(self, tools: list[dict], system_prompt: str) -> types.GenerateContentConfig:
        kwargs: dict = {
            "system_instruction": system_prompt,
            "max_output_tokens": self.max_tokens,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if tools:
            kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t.get("description", ""),
                            parameters_json_schema=t.get("input_schema", {"type": "object", "properties": {}}),
                        )
                        for t in tools
                    ]
                )
            ]
        if self.thinking_mode == "adaptive":
            kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
        elif self.thinking_mode == "enabled":
            kwargs["thinking_config"] = types.ThinkingConfig(
                include_thoughts=True, thinking_budget=self.thinking_budget_tokens
            )
        else:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _turn_key(content: list[dict] | str) -> str:
        if isinstance(content, str):
            return "text:" + content
        for block in content:
            if block.get("type") == "tool_use":
                return "call:" + block["id"]
        return "text:" + "\n".join(b.get("text", "") for b in content if b.get("type") == "text")

    def _to_contents(self, messages: list[dict]) -> list[types.Content]:
        names: dict[str, str] = {}
        contents: list[types.Content] = []
        for msg in messages:
            role, content = msg["role"], msg["content"]
            if role == "assistant":
                if isinstance(content, list):
                    for b in content:
                        if b.get("type") == "tool_use":
                            names[b["id"]] = b["name"]
                raw = self._raw_turns.get(self._turn_key(content))
                if raw is not None:
                    contents.append(raw)
                    continue
                parts: list[types.Part] = []
                if isinstance(content, str):
                    parts.append(types.Part(text=content))
                else:
                    for b in content:
                        if b.get("type") == "text" and b.get("text"):
                            parts.append(types.Part(text=b["text"]))
                        elif b.get("type") == "tool_use":
                            parts.append(types.Part(function_call=types.FunctionCall(
                                id=self._call_ids.get(b["id"], b["id"]), name=b["name"], args=b["input"],
                            )))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            parts = []
            if isinstance(content, str):
                parts.append(types.Part(text=content))
            else:
                for b in content:
                    if b.get("type") == "tool_result":
                        tid = b["tool_use_id"]
                        parts.append(types.Part(function_response=types.FunctionResponse(
                            id=self._call_ids.get(tid, tid),
                            name=names.get(tid, "tool"),
                            response={"result": str(b.get("content", ""))},
                        )))
                    elif b.get("type") == "text" and b.get("text"):
                        parts.append(types.Part(text=b["text"]))
            if parts:
                contents.append(types.Content(role="user", parts=parts))
        return contents

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    async def send(self, messages: list[dict], tools: list[dict], system_prompt: str) -> ProviderResponse:
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=self._to_contents(messages),
                config=self._config(tools, system_prompt),
            )
        except Exception as e:
            raise ProviderError(str(e)) from e

        candidates = getattr(response, "candidates", None) or []
        if not candidates or candidates[0].content is None:
            reason = ""
            feedback = getattr(response, "prompt_feedback", None)
            if feedback is not None and getattr(feedback, "block_reason", None):
                reason = f" ({feedback.block_reason})"
            return ProviderResponse(content=[TextBlock(text=f"The model returned no answer{reason}.")])

        raw = candidates[0].content
        blocks: list = []
        for part in raw.parts or []:
            call = getattr(part, "function_call", None)
            if call is not None:
                our_id = call.id or f"call_{uuid.uuid4().hex[:12]}"
                self._call_ids[our_id] = call.id
                blocks.append(ToolUseBlock(id=our_id, name=call.name, input=dict(call.args or {})))
            elif getattr(part, "text", None):
                if getattr(part, "thought", False):
                    blocks.append(ThinkingBlock(thinking=part.text, signature=""))
                else:
                    blocks.append(TextBlock(text=part.text))

        if not blocks:
            blocks.append(TextBlock(text=""))

        # Remember the reply exactly as received (thought signatures included).
        key_content = [
            {"type": "tool_use", "id": b.id} if isinstance(b, ToolUseBlock)
            else {"type": "text", "text": b.text} if isinstance(b, TextBlock)
            else {"type": "thinking"}
            for b in blocks
        ]
        self._raw_turns[self._turn_key(key_content)] = raw
        return ProviderResponse(content=blocks)
