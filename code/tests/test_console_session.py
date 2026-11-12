"""Tests for the agent's console controls (plan, pause, stop) and a full WebSocket session."""

import asyncio
import shutil

import pytest
from fastapi.testclient import TestClient

from nano_agent.agent import PLAN_REJECTED_MESSAGE, STOPPED_MESSAGE, Agent
from nano_agent.config import AgentConfig
from nano_agent.events import EventBus, Paused, PlanProposed, PostToolUse, PreToolUse
from nano_agent.providers.base import Provider, ProviderResponse, TextBlock, ToolUseBlock
from nano_agent.server import create_app
from nano_agent.store import Store


class Scripted(Provider):
    """Returns scripted replies; records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def send(self, messages, tools, system_prompt):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools]})
        return self.replies.pop(0)


def plan_reply(*steps):
    return ProviderResponse(content=[ToolUseBlock(id="p1", name="propose_plan", input={"steps": list(steps)})])


def echo_tools(log):
    async def echo(message: str) -> str:
        log.append(message)
        return f"echoed {message}"
    return {"echo": {"function": echo, "schema": {"name": "echo", "description": "Echo", "input_schema": {
        "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}}}


# ---------- agent controls ----------

def test_plan_first_waits_for_approval_then_acts():
    log, plans = [], []
    provider = Scripted([
        plan_reply({"action": "Echo hi", "why": "asked to"}),
        ProviderResponse(content=[ToolUseBlock(id="t1", name="echo", input={"message": "hi"})]),
        ProviderResponse(content=[TextBlock(text="Done")]),
    ])
    bus = EventBus()

    async def approve_plan(e: PlanProposed):
        plans.append(e.steps)
        return True, "keep it short"

    bus.on_plan(approve_plan)
    agent = Agent(provider, bus, tools=echo_tools(log), plan_first=True)
    assert asyncio.run(agent.run("say hi")) == "Done"
    assert plans == [[{"action": "Echo hi", "why": "asked to"}]]
    assert provider.calls[0]["tools"] == ["propose_plan"]
    assert log == ["hi"]
    assert "keep it short" in agent.history[2]["content"][0]["content"]


def test_rejected_plan_runs_nothing():
    log = []
    provider = Scripted([plan_reply({"action": "Echo", "why": "x"})])
    bus = EventBus()

    async def reject(_e):
        return False

    bus.on_plan(reject)
    agent = Agent(provider, bus, tools=echo_tools(log), plan_first=True)
    assert asyncio.run(agent.run("go")) == PLAN_REJECTED_MESSAGE
    assert log == []
    assert agent.history[-1]["role"] == "assistant"


def test_pause_stops_before_next_tool_and_note_reaches_model():
    log, events = [], []
    provider = Scripted([
        ProviderResponse(content=[
            ToolUseBlock(id="t1", name="echo", input={"message": "one"}),
            ToolUseBlock(id="t2", name="echo", input={"message": "two"}),
        ]),
        ProviderResponse(content=[TextBlock(text="Done")]),
    ])
    bus = EventBus()
    agent = Agent(provider, bus, tools=echo_tools(log))

    async def after_tool(e: PostToolUse):
        if e.tool_id == "t1":
            agent.request_pause()

    async def on_paused(_e: Paused):
        events.append(("paused", list(log)))
        asyncio.get_running_loop().call_later(0.01, agent.resume, "use capitals")

    bus.on(PostToolUse, after_tool)
    bus.on(Paused, on_paused)
    asyncio.run(agent.run("go"))

    assert events == [("paused", ["one"])]
    assert log == ["one", "two"]
    last_user = provider.calls[1]["messages"][-1]["content"]
    assert last_user[-1] == {"type": "text", "text": "Note from the user: use capitals"}


def test_stop_skips_remaining_tools_and_keeps_history_valid():
    log = []
    provider = Scripted([
        ProviderResponse(content=[
            ToolUseBlock(id="t1", name="echo", input={"message": "one"}),
            ToolUseBlock(id="t2", name="echo", input={"message": "two"}),
        ]),
    ])
    bus = EventBus()
    agent = Agent(provider, bus, tools=echo_tools(log))

    async def stop_after_first(e: PostToolUse):
        if e.tool_id == "t1":
            agent.request_stop()

    bus.on(PostToolUse, stop_after_first)
    assert asyncio.run(agent.run("go")) == STOPPED_MESSAGE
    assert log == ["one"]
    results = agent.history[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[1]["content"] == "Stopped by user"


def test_reason_is_passed_to_approval():
    seen = []
    provider = Scripted([
        ProviderResponse(content=[TextBlock(text="It is unused."), ToolUseBlock(id="t1", name="echo", input={"message": "x"})]),
        ProviderResponse(content=[TextBlock(text="ok")]),
    ])
    bus = EventBus()

    async def approve(e: PreToolUse):
        seen.append((e.tool_id, e.reason))
        return True

    bus.on_approval(approve)
    asyncio.run(Agent(provider, bus, tools=echo_tools([])).run("go"))
    assert seen == [("t1", "It is unused.")]


# ---------- the app: projects, chats and a full WebSocket session ----------

def recv_until(ws, match, limit=80):
    """Read messages until match(msg) is true."""
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if match(msg):
            return msg, seen
    raise AssertionError(f"no matching message; got {[m['type'] for m in seen]}")


def item_where(**want):
    def match(msg):
        item = msg.get("item") or {}
        return msg["type"] == "item" and all(item.get(k) == v for k, v in want.items())
    return match


def make_project(root):
    root.mkdir()
    (root / "main.py").write_text('CONFIG = load_config("config_old.json")\n')
    (root / "utils.py").write_text("def a():\n    return 1\n\ndef old_helper(x):\n    return x * 2\n")
    (root / "config_old.json").write_text("{}\n")
    return root


def test_projects_and_browse_api(tmp_path):
    proj = make_project(tmp_path / "proj")
    app = create_app(Store(tmp_path / "home"), AgentConfig(), provider_factory=lambda _c: Scripted([]))
    client = TestClient(app)

    added = client.post("/api/projects", json={"path": str(proj)}).json()
    assert added["name"] == "proj"
    assert client.post("/api/projects", json={"path": str(proj)}).json()["id"] == added["id"]  # no duplicates
    assert client.post("/api/projects", json={"path": str(tmp_path / "nope")}).status_code == 400

    created = client.post("/api/projects/create", json={"parent": str(tmp_path), "name": "fresh"}).json()
    assert (tmp_path / "fresh").is_dir() and created["name"] == "fresh"
    assert client.post("/api/projects/create", json={"parent": str(tmp_path), "name": "a/b"}).status_code == 400

    state = client.get("/api/state").json()
    assert {p["name"] for p in state["projects"]} == {"proj", "fresh"}
    assert "main.py" in client.get(f"/api/projects/{added['id']}/entries").json()["entries"]

    listing = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert "proj" in listing["dirs"] and listing["places"]

    client.delete(f"/api/projects/{created['id']}")
    assert (tmp_path / "fresh").is_dir()  # removing a project never deletes its folder
    assert [p["name"] for p in client.get("/api/state").json()["projects"]] == ["proj"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_chat_session_end_to_end(tmp_path):
    proj = make_project(tmp_path / "proj")
    provider = Scripted([
        plan_reply({"action": "Remove old_helper", "target": "utils.py", "why": "unused"},
                   {"action": "Delete config_old.json", "why": "looks unused"}),
        ProviderResponse(content=[ToolUseBlock(id="r1", name="read_file", input={"file_path": "main.py"})]),
        ProviderResponse(content=[TextBlock(text="old_helper is never called."), ToolUseBlock(id="e1", name="edit_file", input={
            "file_path": "utils.py", "old_string": "\ndef old_helper(x):\n    return x * 2\n", "new_string": ""})]),
        ProviderResponse(content=[ToolUseBlock(id="d1", name="delete_file", input={"file_path": "config_old.json"})]),
        ProviderResponse(content=[TextBlock(text="Cleaned up utils.py.")]),
        # follow-up message in the same chat
        ProviderResponse(content=[TextBlock(text="utils.py now has one function.")]),
    ])
    store = Store(tmp_path / "home")
    project = store.add_project(proj)
    app = create_app(store, AgentConfig(), provider_factory=lambda _c: provider)
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "send", "project_id": project["id"], "text": "Clean up unused files", "plan_first": True, "ref": "r"})
        created, _ = recv_until(ws, lambda m: m["type"] == "chat_created")
        chat_id = created["chat_id"]
        assert created["ref"] == "r"

        plan, _ = recv_until(ws, item_where(kind="plan", status="pending"))
        assert plan["item"]["steps"][0]["target"] == "utils.py"
        ws.send_json({"type": "plan_decision", "chat_id": chat_id, "approve": True})

        edit, seen = recv_until(ws, item_where(kind="step", status="waiting"))
        reads = [m["item"] for m in seen if m["type"] == "item" and m["item"].get("tool") == "read_file"]
        assert reads and reads[0]["tag"] == "auto"  # reads run on their own
        assert edit["item"]["reason"] == "old_helper is never called."
        assert any(row[0] == "-" and "old_helper" in row[1] for row in edit["item"]["approval"]["diff"])
        ws.send_json({"type": "decision", "chat_id": chat_id, "item_id": edit["item"]["id"], "action": "approve"})

        delete, _ = recv_until(ws, item_where(kind="step", status="waiting", tool="delete_file"))
        assert delete["item"]["approval"]["used_by"][0].startswith("main.py, line 1")
        ws.send_json({"type": "decision", "chat_id": chat_id, "item_id": delete["item"]["id"], "action": "skip"})

        summary, _ = recv_until(ws, item_where(kind="summary"))
        assert summary["item"]["changes"] == [{"status": "Changed", "path": "utils.py"}]
        assert (proj / "config_old.json").exists()
        assert "old_helper" not in (proj / "utils.py").read_text()

        # follow-up in the same chat keeps the conversation going
        ws.send_json({"type": "send", "chat_id": chat_id, "text": "What is left in utils.py?", "plan_first": False})
        reply, _ = recv_until(ws, item_where(kind="agent", text="utils.py now has one function."))
        assert reply
        last_call = provider.calls[-1]["messages"]
        assert last_call[0]["content"] == "Clean up unused files"
        assert last_call[-1]["content"] == "What is left in utils.py?"
        recv_until(ws, lambda m: m["type"] == "state" and m["chat_id"] == chat_id and not m["running"])

        ws.send_json({"type": "undo", "chat_id": chat_id, "item_id": edit["item"]["id"]})
        undone, _ = recv_until(ws, item_where(kind="step", status="undone"))
        assert undone["item"]["id"] == edit["item"]["id"]
        assert "old_helper" in (proj / "utils.py").read_text()

    # the chat was saved and can be reopened
    saved = client.get(f"/api/chats/{chat_id}").json()
    kinds = [i["kind"] for i in saved["items"]]
    assert kinds[0] == "user" and "plan" in kinds and "summary" in kinds and "agent" in kinds
    assert saved["chat"]["title"] == "Clean up unused files"
    assert store.chat(chat_id)["history"][0] == {"role": "user", "content": "Clean up unused files"}


def test_direct_answer_skips_plan_approval():
    provider = Scripted([ProviderResponse(content=[TextBlock(text="Python is a language.")])])
    bus = EventBus()
    plans = []

    async def on_plan(e):
        plans.append(e)
        return True

    bus.on_plan(on_plan)
    agent = Agent(provider, bus, tools={}, plan_first=True)
    assert asyncio.run(agent.run("What is Python?")) == "Python is a language."
    assert plans == []
    assert agent.history[-1]["role"] == "assistant"
