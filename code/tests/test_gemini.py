"""Tests for the Gemini provider with a mocked google-genai client (no API key needed)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from nano_agent.providers.base import ProviderError, TextBlock, ThinkingBlock, ToolUseBlock
from nano_agent.providers.gemini import GeminiProvider


def _client(*replies):
    """A fake client whose generate_content returns the given Content objects in order."""
    responses = [SimpleNamespace(candidates=[SimpleNamespace(content=c)], prompt_feedback=None) for c in replies]
    gen = AsyncMock(side_effect=responses)
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=gen))), gen


TOOLS = [{"name": "read_file", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}]


def test_text_reply_and_request_shape():
    client, gen = _client(types.Content(role="model", parts=[types.Part(text="Hello")]))
    provider = GeminiProvider(model="gemini-test", max_tokens=1234, client=client)

    result = asyncio.run(provider.send([{"role": "user", "content": "hi"}], TOOLS, "Be brief."))

    assert result.content == [TextBlock(text="Hello")]
    kwargs = gen.call_args.kwargs
    assert kwargs["model"] == "gemini-test"
    config = kwargs["config"]
    assert config.system_instruction == "Be brief."
    assert config.max_output_tokens == 1234
    decl = config.tools[0].function_declarations[0]
    assert decl.name == "read_file"
    assert decl.parameters_json_schema["required"] == ["file_path"]
    assert kwargs["contents"][0].role == "user"
    assert kwargs["contents"][0].parts[0].text == "hi"


def test_function_call_gets_an_id_and_thoughts_become_thinking():
    reply = types.Content(role="model", parts=[
        types.Part(text="I should look first.", thought=True),
        types.Part(function_call=types.FunctionCall(name="read_file", args={"file_path": "a.py"})),
    ])
    client, _ = _client(reply)
    provider = GeminiProvider(client=client)

    result = asyncio.run(provider.send([{"role": "user", "content": "read a.py"}], TOOLS, ""))

    assert isinstance(result.content[0], ThinkingBlock)
    assert result.content[0].thinking == "I should look first."
    call = result.content[1]
    assert isinstance(call, ToolUseBlock)
    assert call.name == "read_file" and call.input == {"file_path": "a.py"}
    assert call.id.startswith("call_")


def test_history_replays_original_reply_and_maps_tool_results():
    first = types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(id="fc1", name="read_file", args={"file_path": "a.py"}),
                   thought_signature=b"sig"),
    ])
    second = types.Content(role="model", parts=[types.Part(text="Done")])
    client, gen = _client(first, second)
    provider = GeminiProvider(client=client)

    r1 = asyncio.run(provider.send([{"role": "user", "content": "go"}], TOOLS, ""))
    call = r1.content[0]
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": "x = 1"},
            {"type": "text", "text": "Note from the user: be careful"},
        ]},
    ]
    asyncio.run(provider.send(history, TOOLS, ""))

    contents = gen.call_args.kwargs["contents"]
    assert contents[1] is first  # replayed exactly, thought signature included
    response_part = contents[2].parts[0].function_response
    assert response_part.id == "fc1"
    assert response_part.name == "read_file"
    assert response_part.response == {"result": "x = 1"}
    assert contents[2].parts[1].text == "Note from the user: be careful"


def test_saved_state_restores_signatures():
    first = types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(id="fc1", name="read_file", args={"file_path": "a.py"}),
                   thought_signature=b"sig"),
    ])
    client, _ = _client(first)
    provider = GeminiProvider(client=client)
    call = asyncio.run(provider.send([{"role": "user", "content": "go"}], TOOLS, "")).content[0]

    import json
    state = json.loads(json.dumps(provider.export_state()))  # what the chat file stores
    client2, gen2 = _client(types.Content(role="model", parts=[types.Part(text="ok")]))
    restored = GeminiProvider(client=client2)
    restored.import_state(state)
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": "x"}]},
    ]
    asyncio.run(restored.send(history, TOOLS, ""))
    replayed = gen2.call_args.kwargs["contents"][1]
    assert replayed.parts[0].thought_signature == b"sig"


def test_empty_reply_becomes_message():
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(
        return_value=SimpleNamespace(candidates=[], prompt_feedback=SimpleNamespace(block_reason="SAFETY"))))))
    provider = GeminiProvider(client=client)
    result = asyncio.run(provider.send([{"role": "user", "content": "hi"}], [], ""))
    assert "no answer" in result.content[0].text
    assert "SAFETY" in result.content[0].text


def test_api_error_becomes_provider_error():
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(side_effect=RuntimeError("quota")))))
    provider = GeminiProvider(client=client)
    with pytest.raises(ProviderError, match="quota"):
        asyncio.run(provider.send([{"role": "user", "content": "hi"}], [], ""))


def test_thinking_modes():
    client, gen = _client(types.Content(role="model", parts=[types.Part(text="ok")]))
    asyncio.run(GeminiProvider(client=client, thinking_mode="disabled").send([{"role": "user", "content": "hi"}], [], ""))
    assert gen.call_args.kwargs["config"].thinking_config.thinking_budget == 0
    with pytest.raises(ValueError, match="required"):
        GeminiProvider(client=client, thinking_mode="enabled")
