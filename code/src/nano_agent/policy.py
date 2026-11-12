"""Risk policy for the oversight console: which tool calls run on their own and which ask first.

Defaults come from the team's survey (n = 12, question 13): an action asks first when
most respondents said "ask me first". A tie goes to "don't ask", because 9 of 12 said
they would stop reading approvals that come too often (question 15). Deleting (12/12)
and pushing (9/12, permanent) always ask and cannot be switched off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SETTINGS = {
    "edit": True,      # 10/12 ask first
    "delete": True,    # 12/12, locked
    "secret": True,    # 11/12
    "install": True,   # 7/12
    "command": False,  # 6/12 tie -> fewer prompts
    "push": True,      # 9/12, permanent, locked
}
LOCKED = {"delete", "push"}

READ_TOOLS = {"read_file", "list_directory", "find_files"}
EDIT_TOOLS = {"edit_file", "write_file"}

_PUSH = re.compile(r"\bgit\s+push\b")
_DESTRUCTIVE = re.compile(
    r"(^|[;&|(]\s*)(rm|rmdir|del|erase|rd|Remove-Item)\b|\bgit\s+(reset\s+--hard|clean\b|checkout\s+--\s)",
    re.IGNORECASE,
)
_INSTALL = re.compile(
    r"\b(pip3?|uv|poetry)\s+(install|add)\b|\b(npm|pnpm)\s+(install|i|add)\b|\byarn\s+add\b|\bapt(-get)?\s+install\b",
    re.IGNORECASE,
)
_SECRET = re.compile(r"^\.env|secret|credential|^(config|settings)[^/\\]*\.(json|ya?ml|toml|ini|cfg|env)$", re.IGNORECASE)


@dataclass
class Decision:
    kind: str                 # read, edit, secret, delete, command, install, push, destructive, other
    decision: str             # auto, ask, block
    tag: str                  # auto, ask, choice, perm, blocked, allowed
    permanent: bool = False
    undoable: bool = False
    target: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


def target_of(tool_name: str, params: dict) -> str:
    if tool_name == "run_bash":
        return str(params.get("command", ""))
    return str(params.get("file_path") or params.get("path") or params.get("pattern") or ".")


def kind_of(tool_name: str, params: dict) -> str:
    if tool_name in READ_TOOLS:
        return "read"
    if tool_name in EDIT_TOOLS:
        name = Path(str(params.get("file_path", ""))).name
        return "secret" if _SECRET.search(name) else "edit"
    if tool_name == "delete_file":
        return "delete"
    if tool_name == "run_bash":
        cmd = str(params.get("command", ""))
        if _PUSH.search(cmd):
            return "push"
        if _DESTRUCTIVE.search(cmd):
            return "destructive"
        if _INSTALL.search(cmd):
            return "install"
        return "command"
    return "other"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def in_scope(tool_name: str, params: dict, workspace: Path, scope: list[str] | None) -> tuple[bool, str]:
    """Check a file-touching call against the workspace and the user's chosen files."""
    if tool_name == "run_bash":
        return True, ""
    raw = params.get("file_path") or params.get("path") or "."
    ws = workspace.resolve()
    full = (ws / str(raw)).resolve()
    if not _inside(full, ws):
        return False, "Outside the workspace"
    if _inside(full, ws / ".nano-agent"):
        return False, "The console's own undo history is off limits"
    if scope is None or tool_name in {"list_directory", "find_files"} and full == ws:
        return True, ""
    for item in scope:
        allowed = (ws / item).resolve()
        if full == allowed or _inside(full, allowed):
            return True, ""
    return False, "Outside the files you chose"


def classify(
    tool_name: str,
    params: dict,
    *,
    workspace: Path,
    settings: dict | None = None,
    scope: list[str] | None = None,
    allowed_for_task: set[str] | None = None,
) -> Decision:
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    for key in LOCKED:
        settings[key] = True
    allowed_for_task = allowed_for_task or set()
    kind = kind_of(tool_name, params)
    target = target_of(tool_name, params)

    ok, why = in_scope(tool_name, params, workspace, scope)
    if not ok:
        return Decision(kind, "block", "blocked", target=target, note=why)

    if kind == "read":
        return Decision(kind, "auto", "auto", target=target)
    if kind == "push":
        return Decision(kind, "ask", "perm", permanent=True, target=target)
    if kind == "destructive":
        return Decision(kind, "ask", "perm", permanent=True, target=target,
                        note="Shell deletes skip the trash and may not be undoable.")
    undoable = kind in {"edit", "secret", "delete"}
    if kind == "delete":
        return Decision(kind, "ask", "ask", undoable=True, target=target)
    if kind in allowed_for_task:
        return Decision(kind, "auto", "allowed", undoable=undoable, target=target)
    if kind in {"command", "install", "other"}:
        ask = settings.get(kind, True) if kind != "other" else True
        return Decision(kind, "ask" if ask else "auto", "choice", target=target)
    ask = settings.get(kind, True)
    return Decision(kind, "ask" if ask else "auto", "ask" if ask else "auto", undoable=undoable, target=target)
