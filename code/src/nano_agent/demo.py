"""Demo mode for the console: a scripted model and a small real project to clean up.

``nano-agent-console --demo`` needs no API key. The model's replies are scripted, but
every tool really runs on a throwaway copy of the demo project, so approvals, the
"still used by" warning, failing tests, pause notes and undo all behave for real.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from nano_agent.providers.base import Provider, ProviderResponse, TextBlock, ToolUseBlock

DEMO_FILES = {
    "main.py": (
        "import json\n"
        "\n"
        "from utils import format_name\n"
        "\n"
        "\n"
        "def load_config(path):\n"
        "    with open(path) as f:\n"
        "        return json.load(f)\n"
        "\n"
        "\n"
        "CONFIG = load_config(\"config_old.json\")\n"
        "\n"
        "\n"
        "def greet(name):\n"
        "    return f\"{CONFIG['greeting']}, {format_name(name)}!\"\n"
    ),
    "utils.py": (
        "def format_name(name):\n"
        "    return name.strip().title()\n"
        "\n"
        "\n"
        "def old_helper(x):\n"
        "    return x * 2\n"
    ),
    "config_old.json": "{\"greeting\": \"Hello\"}\n",
    "tests/test_main.py": (
        "import os\n"
        "import sys\n"
        "\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "\n"
        "from main import greet  # noqa: E402\n"
        "\n"
        "\n"
        "def test_greet():\n"
        "    assert greet(\"  ada \") == \"Hello, Ada!\"\n"
    ),
}

DEMO_TASK = "Clean up unused files in this project"


def create_demo_workspace(path: Path | None = None) -> Path:
    """(Re)create the demo project and return its folder."""
    root = Path(path) if path else Path(tempfile.gettempdir()) / "nano-agent-demo"
    root.mkdir(parents=True, exist_ok=True)
    # Clear the contents, not the folder itself: the server may be running inside it.
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    for rel, text in DEMO_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _results(messages: list[dict]) -> dict[str, str]:
    out = {}
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    out[b["tool_use_id"]] = str(b.get("content", ""))
    return out


def _notes(messages: list[dict]) -> str:
    text = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            text += [b.get("text", "") for b in m["content"] if b.get("type") == "text"]
    return " ".join(text).lower()


class DemoProvider(Provider):
    """Plays the clean-up scenario one step per model call."""

    PLAN = [
        {"action": "List the project files", "target": ".", "why": "See what's there"},
        {"action": "Read main.py", "target": "main.py", "why": "Check which files the code uses"},
        {"action": "Remove old_helper()", "target": "utils.py", "why": "Nothing calls it"},
        {"action": "Delete config_old.json", "target": "config_old.json", "why": "The name ends in _old, so it looks unused"},
        {"action": "Run the tests", "target": "python -m pytest -q tests", "why": "Make sure nothing broke"},
        {"action": "Push to GitHub", "target": "git push origin main", "why": "Share the cleanup"},
    ]

    def __init__(self) -> None:
        self.step = 0

    async def send(self, messages, tools, system_prompt) -> ProviderResponse:
        names = {t["name"] for t in tools}
        # Where the current request starts: the last plain-text user message.
        start = max((i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)), default=0)
        already_ran = any(
            m["role"] == "assistant" and isinstance(m["content"], list)
            and any(b.get("type") == "tool_use" and b.get("id", "").startswith("demo_") for b in m["content"])
            for m in messages[:start]
        )
        if already_ran:
            # Follow-up messages: the demo scripts one task, so answer in plain text.
            return ProviderResponse(content=[TextBlock(text=(
                "This demo scripts one task, the clean-up. Here's where it ended: " + self._wrap_up(messages)
                + " Start a new chat to run it again, or use a real model to keep going."
            ))])
        if names == {"propose_plan"}:
            self.step = 0
            return ProviderResponse(content=[
                TextBlock(text="Here is how I'll clean up the project."),
                ToolUseBlock(id="demo_plan", name="propose_plan", input={"steps": self.PLAN}),
            ])

        self.step += 1
        s = self.step
        if s == 1:
            return self._call("demo_list", "list_directory", {"path": "."})
        if s == 2:
            return self._call("demo_read", "read_file", {"file_path": "main.py"})
        if s == 3:
            return self._call("demo_edit", "edit_file", {
                "file_path": "utils.py",
                "old_string": "\n\ndef old_helper(x):\n    return x * 2\n",
                "new_string": "\n",
            }, "old_helper() is never called anywhere, so I'll remove it.")
        if s == 4:
            if any(w in _notes(messages) for w in ("delete", "config", "keep")):
                self.step += 1
                return self._call("demo_test", "run_bash", {"command": "python -m pytest -q tests"},
                                  "Got it. I won't delete config_old.json. Running the tests instead.")
            return self._call("demo_delete", "delete_file", {"file_path": "config_old.json"},
                              "config_old.json has _old in its name, so it looks unused.")
        if s == 5:
            return self._call("demo_test", "run_bash", {"command": "python -m pytest -q tests"},
                              "Now I'll run the tests to check nothing broke.")
        if s == 6:
            return self._call("demo_push", "run_bash", {"command": "git push origin main"},
                              "Everything is done, so I'll share it with your team.")
        return ProviderResponse(content=[TextBlock(text=self._wrap_up(messages))])

    @staticmethod
    def _call(call_id, name, args, reason="") -> ProviderResponse:
        content = [TextBlock(text=reason)] if reason else []
        content.append(ToolUseBlock(id=call_id, name=name, input=args))
        return ProviderResponse(content=content)

    @staticmethod
    def _wrap_up(messages) -> str:
        r = _results(messages)
        lines = []
        edit = r.get("demo_edit", "")
        lines.append("Removed old_helper() from utils.py." if edit.startswith("Successfully") else "Left utils.py as it was.")
        delete = r.get("demo_delete")
        if delete is None:
            lines.append("Kept config_old.json, as your note asked.")
        elif delete.startswith("Moved"):
            lines.append("Moved config_old.json to .trash/.")
        else:
            lines.append("Kept config_old.json because you skipped deleting it.")
        tests = r.get("demo_test", "")
        if "passed" in tests and "failed" not in tests:
            lines.append("The tests pass.")
        elif tests.startswith("Exit code"):
            lines.append("The tests failed. Undo the delete to bring config_old.json back.")
        push = r.get("demo_push", "")
        lines.append("Did not push." if not push.startswith("Exit code: 0") else "Pushed to GitHub.")
        return " ".join(lines)
