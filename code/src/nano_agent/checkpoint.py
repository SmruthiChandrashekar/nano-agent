"""Undo for the oversight console: a private git history of the workspace.

The history lives in ``<workspace>/.nano-agent/checkpoints.git`` and uses the workspace
as its work tree. It never touches the project's own ``.git``. After each step that may
change files, the console takes a snapshot; undoing a step reverts that snapshot.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

EXCLUDES = [".nano-agent/", ".trash/", ".git/", "nano-agent.log", "nano-agent.log.*", "__pycache__/", ".venv/", "node_modules/"]


@dataclass
class UndoResult:
    ok: bool
    message: str


class Checkpoints:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.git_dir = self.workspace / ".nano-agent" / "checkpoints.git"
        self.base: str | None = None

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.workspace}", *args],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if check and proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
        return proc

    def start(self) -> str:
        """Create the private history if needed and record the starting state."""
        if not (self.git_dir / "HEAD").exists():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "-q", str(self.git_dir)], check=True, capture_output=True)
            self._git("config", "core.bare", "false")
            self._git("config", "user.name", "nano-agent console")
            self._git("config", "user.email", "console@nano-agent.local")
            self._git("config", "core.autocrlf", "false")
            info = self.git_dir / "info"
            info.mkdir(exist_ok=True)
            (info / "exclude").write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", "Start of task")
        self.base = self.head()
        return self.base

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def snapshot(self, label: str) -> str | None:
        """Commit the current files. Returns the commit id, or None if nothing changed."""
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        self._git("commit", "-q", "-m", label[:200])
        return self.head()

    def undo(self, commit: str) -> UndoResult:
        """Reverse one snapshot. Later changes stay unless they touched the same lines."""
        self.snapshot("Before undo")
        proc = self._git("revert", "--no-edit", commit, check=False)
        if proc.returncode != 0:
            self._git("revert", "--abort", check=False)
            return UndoResult(False, "A later step changed the same lines, so this step can't be undone on its own. Undo the later step first, or go back to before this step.")
        return UndoResult(True, "Undone")

    def restore_to_before(self, commit: str) -> UndoResult:
        """Put every file back the way it was before this snapshot (undo it and everything after)."""
        self.snapshot("Before going back")
        proc = self._git("read-tree", "-u", "--reset", f"{commit}^", check=False)
        if proc.returncode != 0:
            return UndoResult(False, (proc.stderr or "Could not go back").strip())
        self._git("commit", "-q", "--allow-empty", "-m", f"Went back to before {commit[:8]}")
        return UndoResult(True, "Went back")

    def changes_since_start(self) -> list[dict]:
        """Files that differ from the start of the task: [{status, path}]."""
        if not self.base:
            return []
        self.snapshot("End of task")
        out = self._git("diff", "--name-status", "-M", self.base, "HEAD").stdout
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status = {"A": "Added", "M": "Changed", "D": "Deleted", "R": "Renamed"}.get(parts[0][:1], parts[0])
                rows.append({"status": status, "path": parts[-1] if parts[0].startswith("R") else parts[1]})
        return rows
