"""Delete file tool - moves the file into .trash/ so it can be restored."""

import os
import shutil
import time

TRASH_DIR = ".trash"

SCHEMA = {
    "name": "delete_file",
    "description": "Delete a file. The file is moved to .trash/ in the current folder so it can be restored.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path of the file to delete.",
            },
        },
        "required": ["file_path"],
    },
}


async def delete_file(file_path: str, root: str | None = None) -> str:
    """Move file_path into <root>/.trash/, keeping its relative path. ``root`` defaults to the current folder."""
    root = root or os.getcwd()
    full = file_path if os.path.isabs(file_path) else os.path.join(root, file_path)
    if not os.path.isfile(full):
        return f"Error: {file_path} is not a file"
    rel = os.path.relpath(os.path.abspath(full), root)
    if rel.startswith(".."):
        rel = os.path.basename(full)
    dest = os.path.join(root, TRASH_DIR, rel)
    shown = os.path.join(TRASH_DIR, rel)
    file_path, display = full, file_path
    if os.path.exists(dest):
        stamp = int(time.time())
        dest, shown = f"{dest}.{stamp}", f"{shown}.{stamp}"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(file_path, dest)
    except OSError as e:
        return f"Error: {e}"
    return f"Moved {display} to {shown.replace(os.sep, '/')}"
