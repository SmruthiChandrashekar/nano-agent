"""Projects and chats for the console app, saved as small JSON files.

Everything lives under one folder (default ``~/.nano-agent``, or ``NANO_AGENT_HOME``):

    projects.json          the imported folders
    chats/<chat-id>.json   one file per chat: what the user saw, plus the model history
"""

from __future__ import annotations

import json
import os
import string
import threading
import time
import uuid
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("NANO_AGENT_HOME") or Path.home() / ".nano-agent")


def _now() -> float:
    return round(time.time(), 3)


class Store:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else default_home()
        self.chats_dir = self.home / "chats"
        self.chats_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- files
    def _read(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, path: Path, data) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------- projects
    def projects(self) -> list[dict]:
        return self._read(self.home / "projects.json", [])

    def project(self, project_id: str) -> dict | None:
        return next((p for p in self.projects() if p["id"] == project_id), None)

    def add_project(self, path: str | Path, name: str | None = None, extra: dict | None = None) -> dict:
        folder = Path(path).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError(f"Folder not found: {folder}")
        with self._lock:
            projects = self.projects()
            for p in projects:
                if Path(p["path"]) == folder:
                    if extra:
                        p.update(extra)
                        self._write(self.home / "projects.json", projects)
                    return p
            project = {"id": uuid.uuid4().hex[:10], "name": name or folder.name or str(folder), "path": str(folder),
                       "added": _now(), **(extra or {})}
            projects.append(project)
            self._write(self.home / "projects.json", projects)
            return project

    def create_project(self, parent: str | Path, name: str) -> dict:
        name = name.strip()
        if not name or any(c in name for c in '\\/:*?"<>|') or name in {".", ".."}:
            raise ValueError("Use a folder name without \\ / : * ? \" < > |")
        folder = Path(parent).expanduser().resolve() / name
        if folder.exists():
            raise ValueError(f"{folder} already exists")
        folder.mkdir(parents=True)
        return self.add_project(folder)

    def remove_project(self, project_id: str) -> None:
        """Forget a project and its chats. Never deletes the folder itself."""
        with self._lock:
            projects = [p for p in self.projects() if p["id"] != project_id]
            self._write(self.home / "projects.json", projects)
        for chat in self.chats(project_id):
            self.delete_chat(chat["id"])

    # ---------------------------------------------------------------- chats
    def chats(self, project_id: str | None = None) -> list[dict]:
        out = []
        for f in self.chats_dir.glob("*.json"):
            c = self._read(f, None)
            if not c or (project_id and c.get("project_id") != project_id):
                continue
            out.append({k: c.get(k) for k in ("id", "project_id", "title", "created", "updated")})
        return sorted(out, key=lambda c: c.get("updated") or 0, reverse=True)

    def new_chat(self, project_id: str, title: str) -> dict:
        chat = {"id": uuid.uuid4().hex[:12], "project_id": project_id, "title": title[:60] or "New chat",
                "created": _now(), "updated": _now(), "items": [], "history": [], "provider_state": None}
        self.save_chat(chat)
        return chat

    def chat(self, chat_id: str) -> dict | None:
        if not chat_id or not all(c in string.hexdigits for c in chat_id):
            return None
        return self._read(self.chats_dir / f"{chat_id}.json", None)

    def save_chat(self, chat: dict) -> None:
        chat["updated"] = _now()
        with self._lock:
            self._write(self.chats_dir / f"{chat['id']}.json", chat)

    def delete_chat(self, chat_id: str) -> None:
        if chat_id and all(c in string.hexdigits for c in chat_id):
            (self.chats_dir / f"{chat_id}.json").unlink(missing_ok=True)


# ------------------------------------------------------------------ folders
def quick_places() -> list[dict]:
    home = Path.home()
    places = [{"name": "Home", "path": str(home)}]
    for sub in ("Desktop", "Documents", "Downloads"):
        if (home / sub).is_dir():
            places.append({"name": sub, "path": str(home / sub)})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                places.append({"name": f"{letter}:", "path": str(drive)})
    else:
        places.append({"name": "/", "path": "/"})
    return places


def browse(path: str | None) -> dict:
    """List the sub-folders of a folder, for the import dialog."""
    folder = Path(path).expanduser().resolve() if path else Path.home()
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    dirs = []
    try:
        for entry in folder.iterdir():
            if entry.name.startswith(".") or entry.name.startswith("$"):
                continue
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
            except OSError:
                continue
    except PermissionError:
        raise ValueError(f"No permission to open {folder}")
    parent = str(folder.parent) if folder.parent != folder else None
    return {"path": str(folder), "parent": parent, "dirs": sorted(dirs, key=str.lower)[:400], "places": quick_places()}
