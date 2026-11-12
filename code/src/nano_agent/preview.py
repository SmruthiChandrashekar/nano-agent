"""What the approval card shows before a tool runs: the diff, and who still uses a file."""

from __future__ import annotations

import difflib
import os
from pathlib import Path

SKIP_DIRS = {".git", ".nano-agent", ".trash", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
MAX_FILE_BYTES = 1_000_000
MAX_DIFF_LINES = 60


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def diff_lines(old: str, new: str) -> list[list[str]]:
    """Unified diff as [sign, text] rows; sign is '-', '+', ' ' or '@'."""
    rows: list[list[str]] = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            rows.append(["@", line])
        else:
            rows.append([line[:1] or " ", line[1:]])
        if len(rows) >= MAX_DIFF_LINES:
            rows.append(["@", "… more changes not shown"])
            break
    return rows


def used_by(workspace: Path, file_path: str, limit: int = 5) -> list[str]:
    """Find lines in other files that mention this file's name (e.g. imports, config loads)."""
    target = (workspace / file_path).resolve()
    names = {target.name}
    if target.suffix == ".py":
        names.add(target.stem)
    hits: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            if path.resolve() == target:
                continue
            text = _read(path)
            if not text:
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if any(n in line for n in names):
                    rel = path.relative_to(workspace).as_posix()
                    hits.append(f"{rel}, line {no}: {line.strip()[:120]}")
                    if len(hits) >= limit:
                        return hits
    return hits


def build_preview(tool_name: str, params: dict, workspace: Path) -> dict:
    """Card details for one tool call. Never raises: problems become a short message."""
    out: dict = {"diff": [], "used_by": [], "what": ""}
    try:
        if tool_name == "edit_file":
            path = workspace / str(params.get("file_path", ""))
            old = _read(path)
            if old is None:
                out["what"] = "The file can't be read, so the change can't be previewed."
            else:
                old_s, new_s = str(params.get("old_string", "")), str(params.get("new_string", ""))
                count = old.count(old_s) if old_s else 0
                if count != 1:
                    out["what"] = "This edit will fail: the text to replace " + ("isn't in the file." if count == 0 else "appears more than once.")
                else:
                    out["diff"] = diff_lines(old, old.replace(old_s, new_s, 1))
        elif tool_name == "write_file":
            path = workspace / str(params.get("file_path", ""))
            new = str(params.get("content", ""))
            old = _read(path) if path.exists() else None
            if path.exists() and old is not None:
                out["diff"] = diff_lines(old, new)
                out["what"] = "Overwrites the whole file."
            else:
                out["diff"] = [["+", line] for line in new.splitlines()[:MAX_DIFF_LINES]]
                out["what"] = f"Creates a new file ({len(new.splitlines())} lines)."
        elif tool_name == "delete_file":
            out["used_by"] = used_by(workspace, str(params.get("file_path", "")))
            out["what"] = "Moves the file to .trash/. You can undo this."
        elif tool_name == "run_bash":
            out["what"] = f"Runs the command: {params.get('command', '')}"
    except Exception as e:  # a preview must never block the approval itself
        out["what"] = f"Preview unavailable: {e}"
    return out
