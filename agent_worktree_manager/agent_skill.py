"""Export the bundled, agent-independent worktree skill."""

from __future__ import annotations

import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from .errors import AWMError

SKILL_FILES = ("SKILL.md", "agents/openai.yaml")


def bundled_files() -> dict[str, bytes]:
    root = files("agent_worktree_manager").joinpath("skills", "awm")
    return {name: root.joinpath(*name.split("/")).read_bytes() for name in SKILL_FILES}


def install_skill(destination: Path) -> Path:
    destination = Path(os.path.abspath(destination.expanduser()))
    content = bundled_files()
    if destination.is_symlink():
        raise AWMError(f"Skill destination must not be a symlink: {destination}")
    if destination.exists():
        if destination.is_dir() and all(
            (destination / name).is_file()
            and not (destination / name).is_symlink()
            and (destination / name).read_bytes() == value
            for name, value in content.items()
        ):
            return destination
        raise AWMError(
            f"Skill destination already exists with different contents: {destination}; "
            "review or move the existing skill before installing"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".awm-skill-", dir=destination.parent))
    try:
        for name, value in content.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
