"""nano-agent app: a local, Claude Code-style web UI on top of the nano-agent loop.

    uv run nano-agent-console            # then open http://127.0.0.1:8765
    uv run nano-agent-console --demo     # no API key: scripted model, real demo project

Projects are folders you import. Each project has chats. In a chat the agent works in
that folder: safe steps run on their own, risky ones wait on an approval card
(policy.py), you can pause or stop at any time, and every change can be undone
(checkpoint.py). Chats are saved (store.py) and can be continued later.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nano_agent.agent import Agent
from nano_agent.checkpoint import Checkpoints
from nano_agent.config import DEFAULT_MODELS, AgentConfig, load_config
from nano_agent.events import EventBus, Paused, PlanProposed, PostToolUse, PreToolUse, Resumed, Stop, Thinking
from nano_agent.listeners import register_logging_listeners
from nano_agent.policy import DEFAULT_SETTINGS, LOCKED, classify
from nano_agent.preview import build_preview
from nano_agent.providers import make_provider
from nano_agent.providers.base import ProviderError
from nano_agent.store import Store, browse
from nano_agent.workspace_tools import bind_tools

STATIC_DIR = Path(__file__).parent / "static"
CHANGING_TOOLS = {"edit_file", "write_file", "delete_file", "run_bash"}
HIDDEN_ENTRIES = {".git", ".nano-agent", ".trash", "__pycache__", ".venv", "node_modules"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _short(text: str, n: int = 400) -> str:
    text = _ANSI.sub("", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _status_of(result: str) -> str:
    if result == "Stopped by user":
        return "notrun"
    if result == "Tool call denied by user":
        return "skipped"
    if result.startswith(("Error", "Unknown tool", "Command timed out")):
        return "failed"
    if result.startswith("Exit code:") and not result.startswith("Exit code: 0"):
        return "failed"
    return "done"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Hub:
    """App-wide state: the store, connected browser tabs, and running chats."""

    def __init__(self, store: Store, config: AgentConfig, provider_factory=make_provider, demo=None) -> None:
        self.store = store
        self.config = config
        self.provider_factory = provider_factory
        self.demo = demo  # optional: callable(workspace) that resets a demo project
        self.sockets: set[WebSocket] = set()
        self.runtimes: dict[str, ChatRuntime] = {}
        self.checkpoints: dict[str, Checkpoints] = {}
        self.git_locks: dict[str, asyncio.Lock] = {}

    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.sockets):
            try:
                await ws.send_json(msg)
            except Exception:
                self.sockets.discard(ws)

    def projects_with_chats(self) -> list[dict]:
        out = []
        for p in self.store.projects():
            chats = self.store.chats(p["id"])
            for c in chats:
                rt = self.runtimes.get(c["id"])
                c["running"] = bool(rt and rt.running)
                c["waiting"] = bool(rt and (rt.waiting_item or rt.plan_future))
            out.append({**p, "exists": Path(p["path"]).is_dir(), "chats": chats})
        return out

    async def chats_changed(self) -> None:
        await self.broadcast({"type": "projects", "projects": self.projects_with_chats()})

    def checkpoints_for(self, project: dict) -> Checkpoints:
        cp = self.checkpoints.get(project["id"])
        if cp is None:
            cp = self.checkpoints[project["id"]] = Checkpoints(Path(project["path"]))
            self.git_locks[project["id"]] = asyncio.Lock()
        return cp

    async def git(self, project: dict, fn, *args):
        self.checkpoints_for(project)
        async with self.git_locks[project["id"]]:
            return await asyncio.to_thread(fn, *args)

    def runtime(self, chat_id: str) -> "ChatRuntime | None":
        rt = self.runtimes.get(chat_id)
        if rt is not None:
            return rt
        chat = self.store.chat(chat_id)
        if not chat:
            return None
        project = self.store.project(chat["project_id"])
        if not project:
            return None
        rt = self.runtimes[chat_id] = ChatRuntime(self, chat, project)
        return rt


class ChatRuntime:
    """One chat: its agent, the items the user sees, approvals, pause and undo."""

    def __init__(self, hub: Hub, chat: dict, project: dict) -> None:
        self.hub = hub
        self.chat = chat
        self.project = project
        self.workspace = Path(project["path"])
        self.agent: Agent | None = None
        self.provider = None
        self.running = False
        self.paused = False
        self.pausing = False
        self.settings = dict(DEFAULT_SETTINGS)
        self.scope: list[str] | None = None
        self.allowed_for_task: set[str] = set()
        self.decisions: dict[str, asyncio.Future] = {}
        self.plan_future: asyncio.Future | None = None
        self.plan_item: dict | None = None
        self.waiting_item: str | None = None
        self.tool_items: dict[str, str] = {}
        self.turn: dict = {}
        self.task: asyncio.Task | None = None
        # A chat loaded from disk is not running: anything left waiting was interrupted.
        for item in self.items:
            if item.get("kind") == "plan" and item.get("status") == "pending":
                item["status"] = "stopped"
            elif item.get("kind") == "step" and item.get("status") in {"running", "waiting"}:
                item["status"] = "notrun"
                item.pop("approval", None)

    # ------------------------------------------------------------ items
    @property
    def items(self) -> list[dict]:
        return self.chat["items"]

    def find(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i.get("id") == item_id), None)

    async def push(self, item: dict, save: bool = True) -> dict:
        item.setdefault("id", _new_id())
        if self.find(item["id"]) is None:
            self.items.append(item)
        if save:
            self.hub.store.save_chat(self.chat)
        await self.hub.broadcast({"type": "item", "chat_id": self.chat["id"], "item": self.public(item)})
        return item

    @staticmethod
    def public(item: dict) -> dict:
        out = {k: v for k, v in item.items() if k != "commit"}
        if item.get("kind") == "step":
            out["undoable"] = bool(item.get("commit")) and item.get("status") in {"done", "failed"}
        return out

    def state(self) -> dict:
        return {"type": "state", "chat_id": self.chat["id"], "running": self.running, "paused": self.paused,
                "pausing": self.pausing, "waiting": self.waiting_item, "plan_pending": bool(self.plan_future)}

    async def send_state(self) -> None:
        await self.hub.broadcast(self.state())

    # ------------------------------------------------------------ agent wiring
    def _ensure_agent(self) -> Agent:
        if self.agent is not None:
            return self.agent
        self.provider = self.hub.provider_factory(self.hub.config)
        if hasattr(self.provider, "import_state"):
            self.provider.import_state(self.chat.get("provider_state"))
        bus = EventBus()
        register_logging_listeners(bus, log_path=self.workspace / ".nano-agent" / "console.log")

        async def on_thinking(e: Thinking) -> None:
            await self.hub.broadcast({"type": "thinking", "chat_id": self.chat["id"], "text": _short(e.text, 300)})

        async def on_plan(e: PlanProposed) -> None:
            self.plan_item = await self.push({"kind": "plan", "steps": e.steps, "text": e.text, "status": "pending"})

        async def on_paused(_e: Paused) -> None:
            self.paused, self.pausing = True, False
            await self.send_state()
            await self.hub.chats_changed()

        async def on_resumed(_e: Resumed) -> None:
            self.paused = False
            await self.send_state()

        async def on_post(e: PostToolUse) -> None:
            await self._after_tool(e)

        async def on_stop(e: Stop) -> None:
            if e.text.strip():
                await self.push({"kind": "agent", "text": e.text})

        for etype, fn in ((Thinking, on_thinking), (PlanProposed, on_plan), (Paused, on_paused),
                          (Resumed, on_resumed), (PostToolUse, on_post), (Stop, on_stop)):
            bus.on(etype, fn)
        bus.on_approval(self._approve)
        bus.on_plan(self._approve_plan)
        self.agent = Agent(provider=self.provider, event_bus=bus, tools=bind_tools(self.workspace),
                           max_turns=self.hub.config.max_turns)
        self.agent.history = self.chat.get("history") or []
        return self.agent

    async def _approve_plan(self, _e: PlanProposed):
        self.plan_future = asyncio.get_running_loop().create_future()
        await self.send_state()
        await self.hub.chats_changed()
        approved, note = await self.plan_future
        self.plan_future = None
        if self.plan_item is not None:
            self.plan_item["status"] = "approved" if approved else "rejected"
            if note:
                self.plan_item["note"] = note
            await self.push(self.plan_item)
        await self.send_state()
        return approved, note

    async def _approve(self, e: PreToolUse) -> bool:
        d = classify(e.tool_name, e.tool_params, workspace=self.workspace, settings=self.settings,
                     scope=self.scope, allowed_for_task=self.allowed_for_task)
        item = {
            "id": _new_id(), "kind": "step", "tool": e.tool_name, "params": e.tool_params, "risk": d.kind,
            "tag": d.tag, "target": d.target, "permanent": d.permanent, "may_undo": d.undoable,
            "reason": _short(e.reason, 600), "status": "running", "result": "", "commit": None, "turn": self.turn.get("id"),
        }
        self.tool_items[e.tool_id] = item["id"]
        if d.decision == "block":
            item.update(status="blocked", result=d.note)
            await self.push(item)
            return False
        if d.decision == "auto":
            await self.push(item, save=False)
            return True

        item["status"] = "waiting"
        item["approval"] = {**build_preview(e.tool_name, e.tool_params, self.workspace), "note": d.note}
        self.waiting_item = item["id"]
        await self.push(item)
        await self.send_state()
        await self.hub.chats_changed()
        fut = asyncio.get_running_loop().create_future()
        self.decisions[item["id"]] = fut
        action, allow = await fut
        self.decisions.pop(item["id"], None)
        self.waiting_item = None
        item["decision"] = action
        if action == "stop" and self.agent:
            self.agent.request_stop()
        if action != "approve":
            await self.send_state()
            return False
        if allow and d.kind not in LOCKED and not d.permanent:
            self.allowed_for_task.add(d.kind)
        item["status"] = "running"
        await self.push(item, save=False)
        await self.send_state()
        return True

    async def _after_tool(self, e: PostToolUse) -> None:
        item = self.find(self.tool_items.get(e.tool_id, ""))
        if item is None:
            return
        status = "blocked" if item["status"] == "blocked" else _status_of(e.result)
        item.update(status=status, result=_short(e.result, 2000))
        if status in {"done", "failed"} and e.tool_name in CHANGING_TOOLS:
            try:
                item["commit"] = await self.hub.git(self.project, self.hub.checkpoints_for(self.project).snapshot,
                                                    f"{e.tool_name} {item['target']}"[:200])
            except Exception:
                item["commit"] = None
        await self.push(item)

    # ------------------------------------------------------------ commands
    async def send(self, text: str, opts: dict) -> None:
        text = text.strip()
        if not text:
            return
        if self.running:
            if self.agent:
                self.agent.add_note(text)
            await self.push({"kind": "note", "who": "user", "text": text})
            if self.paused:
                self.agent.resume()
            return
        if not self.workspace.is_dir():
            await self.push({"kind": "error", "text": f"The project folder is missing: {self.workspace}"})
            return
        self.settings = {**DEFAULT_SETTINGS, **(opts.get("settings") or {})}
        self.scope = opts.get("scope") or None
        self.allowed_for_task = set()
        self.tool_items = {}
        await self.push({"kind": "user", "text": text})
        try:
            agent = self._ensure_agent()
            cp = self.hub.checkpoints_for(self.project)
            base = await self.hub.git(self.project, cp.start)
        except Exception as e:
            await self.push({"kind": "error", "text": f"Could not start: {e}"})
            return
        agent.plan_first = bool(opts.get("plan_first", True))
        self.turn = {"id": _new_id(), "base": base}
        self.running, self.paused, self.pausing = True, False, False
        await self.send_state()
        await self.hub.chats_changed()
        self.task = asyncio.create_task(self._run(text))

    async def _run(self, text: str) -> None:
        try:
            await self.agent.run(text)
        except ProviderError as e:
            await self.push({"kind": "error", "text": f"The model call failed: {_short(str(e), 600)}"})
        except Exception as e:
            await self.push({"kind": "error", "text": f"Something went wrong: {_short(str(e), 600)}"})
        finally:
            if self.agent and self.agent.history and self.agent.history[-1]["role"] == "user":
                # The turn ended early (an error); close it so the next message keeps the history valid.
                self.agent.history.append({"role": "assistant", "content": "(This request ended early.)"})
            self.running = self.paused = self.pausing = False
            self.waiting_item = None
            await self._summary()
            self.chat["history"] = self.agent.history if self.agent else self.chat.get("history", [])
            if self.provider is not None and hasattr(self.provider, "export_state"):
                self.chat["provider_state"] = self.provider.export_state()
            self.hub.store.save_chat(self.chat)
            await self.send_state()
            await self.hub.chats_changed()

    async def _changes(self, base: str | None) -> list[dict]:
        cp = self.hub.checkpoints_for(self.project)
        if not base:
            return []
        cp.base = base
        try:
            return await self.hub.git(self.project, cp.changes_since_start)
        except Exception:
            return []

    async def _summary(self) -> None:
        turn_id = self.turn.get("id")
        steps = [i for i in self.items if i.get("kind") == "step" and i.get("turn") == turn_id]
        if not steps:
            return
        changes = await self._changes(self.turn.get("base"))
        counts = {s: sum(1 for i in steps if i["status"] == s) for s in ("done", "skipped", "failed", "blocked", "notrun")}
        await self.push({"kind": "summary", "turn": turn_id, "base": self.turn.get("base"), "changes": changes, "counts": counts})

    async def decide(self, item_id: str, action: str, allow: bool) -> None:
        fut = self.decisions.get(item_id)
        if fut and not fut.done():
            fut.set_result((action, allow))

    async def plan_decision(self, approve: bool, note: str) -> None:
        if self.plan_future and not self.plan_future.done():
            self.plan_future.set_result((approve, note))

    async def pause(self) -> None:
        if self.agent and self.running and not self.paused:
            self.agent.request_pause()
            self.pausing = True
            await self.send_state()

    async def resume(self, note: str = "") -> None:
        if note:
            await self.push({"kind": "note", "who": "user", "text": note})
        if self.agent:
            self.agent.resume(note or None)

    async def stop(self) -> None:
        if not (self.agent and self.running):
            return
        self.agent.request_stop()
        for fut in list(self.decisions.values()):
            if not fut.done():
                fut.set_result(("stop", False))
        if self.plan_future and not self.plan_future.done():
            self.plan_future.set_result((False, "Stopped"))

    async def undo(self, item_id: str, go_back: bool) -> None:
        item = self.find(item_id)
        if not item or not item.get("commit"):
            await self.hub.broadcast({"type": "error", "chat_id": self.chat["id"], "message": "That step has nothing to undo."})
            return
        cp = self.hub.checkpoints_for(self.project)
        if go_back:
            result = await self.hub.git(self.project, cp.restore_to_before, item["commit"])
            start = self.items.index(item)
            targets = [i for i in self.items[start:] if i.get("kind") == "step" and i.get("commit") and i["status"] in {"done", "failed"}]
        else:
            result = await self.hub.git(self.project, cp.undo, item["commit"])
            targets = [item]
        if not result.ok:
            await self.hub.broadcast({"type": "error", "chat_id": self.chat["id"], "message": result.message})
            return
        names = []
        for t in targets:
            t.update(status="undone", commit=None)
            names.append(f"{t['tool']} {t['target']}")
            await self.push(t, save=False)
        if self.agent and self.running:
            self.agent.add_note("I undid these steps, so their changes are gone: " + "; ".join(names))
        await self.push({"kind": "note", "who": "agent", "text": "Undone: " + "; ".join(names)})
        for s in reversed(self.items):
            if s.get("kind") == "summary":
                s["changes"] = await self._changes(s.get("base"))
                await self.push(s)
                break

    async def close(self) -> None:
        await self.stop()


# ---------------------------------------------------------------- HTTP models
class AddProject(BaseModel):
    path: str


class CreateProject(BaseModel):
    parent: str
    name: str


class Rename(BaseModel):
    title: str


def create_app(store: Store, config: AgentConfig, provider_factory=make_provider, demo=None) -> FastAPI:
    """Build the app. ``demo`` (optional) resets a demo project whenever a new chat starts in it."""
    app = FastAPI(title="nano-agent")
    hub = Hub(store, config, provider_factory, demo)
    app.state.hub = hub

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.html")

    @app.get("/api/state")
    async def state() -> dict:
        return {"projects": hub.projects_with_chats(), "provider": config.provider, "model": config.model,
                "demo": demo is not None, "settings": DEFAULT_SETTINGS, "locked": sorted(LOCKED)}

    @app.get("/api/browse")
    async def browse_folder(path: str | None = None) -> dict:
        try:
            return browse(path)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects")
    async def add_project(body: AddProject) -> dict:
        try:
            project = store.add_project(body.path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        await hub.chats_changed()
        return project

    @app.post("/api/projects/create")
    async def create_project(body: CreateProject) -> dict:
        try:
            project = store.create_project(body.parent, body.name)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        await hub.chats_changed()
        return project

    @app.delete("/api/projects/{project_id}")
    async def remove_project(project_id: str) -> dict:
        for chat in store.chats(project_id):
            rt = hub.runtimes.pop(chat["id"], None)
            if rt:
                await rt.close()
        store.remove_project(project_id)
        await hub.chats_changed()
        return {"ok": True}

    @app.get("/api/projects/{project_id}/entries")
    async def entries(project_id: str) -> dict:
        project = store.project(project_id)
        if not project or not Path(project["path"]).is_dir():
            raise HTTPException(404, "Project folder not found")
        root = Path(project["path"])
        names = sorted((p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name not in HIDDEN_ENTRIES), key=str.lower)
        return {"entries": names[:80]}

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str) -> dict:
        rt = hub.runtime(chat_id)
        if rt is None:
            raise HTTPException(404, "Chat not found")
        return {"chat": {k: rt.chat.get(k) for k in ("id", "project_id", "title", "created", "updated")},
                "items": [rt.public(i) for i in rt.items], "state": rt.state(), "project": rt.project}

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(chat_id: str, body: Rename) -> dict:
        rt = hub.runtime(chat_id)
        if rt is None:
            raise HTTPException(404, "Chat not found")
        rt.chat["title"] = body.title.strip()[:80] or rt.chat["title"]
        store.save_chat(rt.chat)
        await hub.chats_changed()
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str) -> dict:
        rt = hub.runtimes.pop(chat_id, None)
        if rt:
            await rt.close()
        store.delete_chat(chat_id)
        await hub.chats_changed()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        hub.sockets.add(ws)
        try:
            while True:
                await handle(await ws.receive_json(), ws)
        except WebSocketDisconnect:
            hub.sockets.discard(ws)

    async def handle(msg: dict, ws: WebSocket) -> None:
        kind = msg.get("type")
        if kind == "send":
            chat_id = msg.get("chat_id")
            if not chat_id:
                project = store.project(msg.get("project_id", ""))
                if not project:
                    await ws.send_json({"type": "error", "message": "Choose a project first."})
                    return
                if demo is not None and project.get("demo"):
                    await asyncio.to_thread(demo, Path(project["path"]))
                    hub.checkpoints.pop(project["id"], None)
                chat = store.new_chat(project["id"], " ".join(msg.get("text", "").split())[:60])
                chat_id = chat["id"]
                await ws.send_json({"type": "chat_created", "chat_id": chat_id, "project_id": project["id"], "ref": msg.get("ref")})
                await hub.chats_changed()
            rt = hub.runtime(chat_id)
            if rt is None:
                await ws.send_json({"type": "error", "message": "That chat no longer exists."})
                return
            await rt.send(msg.get("text", ""), msg)
            return
        rt = hub.runtime(msg.get("chat_id", ""))
        if rt is None:
            return
        if kind == "decision":
            await rt.decide(msg.get("item_id", ""), msg.get("action", "skip"), bool(msg.get("allow_task")))
        elif kind == "plan_decision":
            await rt.plan_decision(bool(msg.get("approve")), msg.get("note") or "")
        elif kind == "pause":
            await rt.pause()
        elif kind == "resume":
            await rt.resume(msg.get("note") or "")
        elif kind == "stop":
            await rt.stop()
        elif kind == "undo":
            await rt.undo(msg.get("item_id", ""), bool(msg.get("go_back")))
        elif kind == "settings":
            rt.settings = {**DEFAULT_SETTINGS, **(msg.get("settings") or {})}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="nano-agent app (web UI)")
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument("--provider", choices=("gemini", "anthropic"), default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--home", default=None, help="Where projects and chats are saved (default ~/.nano-agent)")
    parser.add_argument("--workspace", default=None, help="Also import this folder as a project")
    parser.add_argument("--demo", action="store_true", help="No API key needed: scripted model on a demo project")
    args = parser.parse_args()

    import uvicorn

    if args.demo:
        import tempfile

        from nano_agent.demo import DemoProvider, create_demo_workspace

        home = Path(args.home) if args.home else Path(tempfile.gettempdir()) / "nano-agent-demo-home"
        store = Store(home)
        workspace = create_demo_workspace()
        store.add_project(workspace, name="Demo project", extra={"demo": True})
        config = AgentConfig(provider="gemini", model="demo (scripted)")
        app = create_app(store, config, provider_factory=lambda _c: DemoProvider(), demo=create_demo_workspace)
        print("nano-agent · DEMO mode (scripted model, real files)")
    else:
        config = load_config(args.config)
        if args.provider and args.provider != config.provider:
            if config.model == DEFAULT_MODELS.get(config.provider):
                config.model = DEFAULT_MODELS[args.provider]
            config.provider = args.provider
        if args.model:
            config.model = args.model
        config.validate()
        store = Store(Path(args.home) if args.home else None)
        if args.workspace:
            store.add_project(args.workspace)
        app = create_app(store, config)
        print(f"nano-agent · {config.provider} / {config.model}")
    print(f"Saved projects and chats: {store.home}")
    print(f"Open http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
