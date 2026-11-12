"""Console tools bound to one project folder.

The model uses paths relative to the project. These wrappers resolve them against the
project folder, so several projects can be open at once without changing the
server's working directory. The schemas the model sees are unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

from nano_agent.tools import get_console_tools
from nano_agent.tools.delete_file import delete_file
from nano_agent.tools.edit_file import edit_file
from nano_agent.tools.find_files import find_files
from nano_agent.tools.list_directory import list_directory
from nano_agent.tools.read_file import read_file
from nano_agent.tools.run_bash import run_bash
from nano_agent.tools.write_file import write_file


def bind_tools(workspace: Path) -> dict:
    ws = Path(workspace).resolve()

    def full(p: str | None) -> str:
        p = p or "."
        return p if os.path.isabs(p) else str(ws / p)

    def relative(text: str) -> str:
        prefix = str(ws) + os.sep
        return "\n".join(line[len(prefix):] if line.startswith(prefix) else line for line in text.split("\n"))

    async def _read(file_path: str) -> str:
        return await read_file(full(file_path))

    async def _edit(file_path: str, old_string: str, new_string: str) -> str:
        return (await edit_file(full(file_path), old_string, new_string)).replace(full(file_path), file_path)

    async def _write(file_path: str, content: str) -> str:
        return (await write_file(full(file_path), content)).replace(full(file_path), file_path)

    async def _find(pattern: str, path: str = ".") -> str:
        return relative(await find_files(pattern, full(path)))

    async def _list(path: str = ".") -> str:
        return await list_directory(full(path))

    async def _bash(command: str, timeout: int = 30) -> str:
        return await run_bash(command, timeout=timeout, cwd=str(ws))

    async def _delete(file_path: str) -> str:
        return await delete_file(file_path, root=str(ws))

    functions = {
        "read_file": _read, "edit_file": _edit, "write_file": _write, "find_files": _find,
        "list_directory": _list, "run_bash": _bash, "delete_file": _delete,
    }
    tools = get_console_tools()
    return {name: {"function": functions[name], "schema": tool["schema"]} for name, tool in tools.items()}
