"""Tests for the console's risk policy, previews, delete tool and undo checkpoints."""

import asyncio
import shutil

import pytest

from nano_agent.checkpoint import Checkpoints
from nano_agent.policy import classify
from nano_agent.preview import build_preview
from nano_agent.tools import get_console_tools
from nano_agent.tools.delete_file import delete_file


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "main.py").write_text('CONFIG = load_config("config_old.json")\n')
    (tmp_path / "utils.py").write_text("def a():\n    return 1\n\ndef old_helper(x):\n    return x * 2\n")
    (tmp_path / "config_old.json").write_text("{}\n")
    (tmp_path / ".env").write_text("KEY=1\n")
    return tmp_path


# ---------- policy (survey Q13 defaults) ----------

@pytest.mark.parametrize("tool,params,decision,tag", [
    ("read_file", {"file_path": "main.py"}, "auto", "auto"),
    ("list_directory", {"path": "."}, "auto", "auto"),
    ("edit_file", {"file_path": "utils.py", "old_string": "a", "new_string": "b"}, "ask", "ask"),
    ("write_file", {"file_path": ".env", "content": "x"}, "ask", "ask"),
    ("delete_file", {"file_path": "config_old.json"}, "ask", "ask"),
    ("run_bash", {"command": "pytest"}, "auto", "choice"),
    ("run_bash", {"command": "pip install requests"}, "ask", "choice"),
    ("run_bash", {"command": "git push origin main"}, "ask", "perm"),
    ("run_bash", {"command": "rm -rf build"}, "ask", "perm"),
])
def test_default_rules(ws, tool, params, decision, tag):
    d = classify(tool, params, workspace=ws)
    assert (d.decision, d.tag) == (decision, tag)


def test_secret_files_are_their_own_kind(ws):
    assert classify("write_file", {"file_path": ".env", "content": ""}, workspace=ws).kind == "secret"


def test_locked_actions_cannot_be_switched_off(ws):
    off = {"delete": False, "push": False, "edit": False}
    assert classify("delete_file", {"file_path": "config_old.json"}, workspace=ws, settings=off).decision == "ask"
    assert classify("run_bash", {"command": "git push"}, workspace=ws, settings=off).decision == "ask"
    assert classify("edit_file", {"file_path": "utils.py"}, workspace=ws, settings=off).decision == "auto"


def test_strict_settings_make_commands_ask(ws):
    d = classify("run_bash", {"command": "pytest"}, workspace=ws, settings={"command": True})
    assert d.decision == "ask"


def test_scope_blocks_other_files_and_outside_paths(ws):
    scope = ["utils.py"]
    assert classify("edit_file", {"file_path": "utils.py"}, workspace=ws, scope=scope).decision == "ask"
    blocked = classify("edit_file", {"file_path": "main.py"}, workspace=ws, scope=scope)
    assert blocked.decision == "block" and "files you chose" in blocked.note
    outside = classify("read_file", {"file_path": "../secret.txt"}, workspace=ws)
    assert outside.decision == "block" and "workspace" in outside.note
    history = classify("write_file", {"file_path": ".nano-agent/checkpoints.git/HEAD", "content": ""}, workspace=ws)
    assert history.decision == "block"


def test_allowed_for_task_skips_asking_but_not_for_deletes(ws):
    allowed = {"edit", "delete"}
    assert classify("edit_file", {"file_path": "utils.py"}, workspace=ws, allowed_for_task=allowed).tag == "allowed"
    assert classify("delete_file", {"file_path": "config_old.json"}, workspace=ws, allowed_for_task=allowed).decision == "ask"


# ---------- previews ----------

def test_edit_preview_shows_removed_lines(ws):
    p = build_preview("edit_file", {"file_path": "utils.py", "old_string": "def old_helper(x):\n    return x * 2\n", "new_string": ""}, ws)
    removed = [row[1] for row in p["diff"] if row[0] == "-"]
    assert "def old_helper(x):" in removed


def test_edit_preview_warns_when_text_missing(ws):
    p = build_preview("edit_file", {"file_path": "utils.py", "old_string": "nope", "new_string": ""}, ws)
    assert "will fail" in p["what"]


def test_delete_preview_finds_users(ws):
    p = build_preview("delete_file", {"file_path": "config_old.json"}, ws)
    assert p["used_by"] and p["used_by"][0].startswith("main.py, line 1")


def test_new_file_preview(ws):
    p = build_preview("write_file", {"file_path": "new.txt", "content": "a\nb\n"}, ws)
    assert p["diff"] == [["+", "a"], ["+", "b"]]


# ---------- delete tool and console tool set ----------

def test_delete_moves_to_trash(ws, monkeypatch):
    monkeypatch.chdir(ws)
    out = asyncio.run(delete_file("config_old.json"))
    assert "Moved" in out
    assert not (ws / "config_old.json").exists()
    assert (ws / ".trash" / "config_old.json").exists()


def test_bound_tools_work_inside_the_project(ws):
    from nano_agent.workspace_tools import bind_tools
    tools = bind_tools(ws)
    assert "old_helper" in asyncio.run(tools["read_file"]["function"]("utils.py"))
    assert asyncio.run(tools["edit_file"]["function"]("utils.py", "return 1", "return 2")) == "Successfully edited utils.py"
    assert "Exit code: 0" in asyncio.run(tools["run_bash"]["function"]("git --version"))
    assert asyncio.run(tools["delete_file"]["function"]("config_old.json")).startswith("Moved config_old.json")
    assert (ws / ".trash" / "config_old.json").exists()


def test_console_tools_have_no_spawn_agent():
    tools = get_console_tools()
    assert "spawn_agent" not in tools
    assert "delete_file" in tools


# ---------- undo checkpoints ----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_undo_single_step_and_go_back(ws, monkeypatch):
    monkeypatch.chdir(ws)
    cp = Checkpoints(ws)
    cp.start()
    (ws / "utils.py").write_text("changed\n")
    edit = cp.snapshot("edit utils.py")
    asyncio.run(delete_file("config_old.json"))
    delete = cp.snapshot("delete config_old.json")
    assert {c["path"] for c in cp.changes_since_start()} == {"utils.py", "config_old.json"}

    assert cp.undo(delete).ok
    assert (ws / "config_old.json").exists()
    assert (ws / "utils.py").read_text() == "changed\n"

    (ws / "b.txt").write_text("later\n")
    cp.snapshot("add b.txt")
    assert cp.restore_to_before(edit).ok
    assert "old_helper" in (ws / "utils.py").read_text()
    assert not (ws / "b.txt").exists()
    assert not (ws / ".git").exists()  # the project's own git is never created or touched


@needs_git
def test_snapshot_without_changes_returns_none(ws):
    cp = Checkpoints(ws)
    cp.start()
    assert cp.snapshot("nothing") is None
