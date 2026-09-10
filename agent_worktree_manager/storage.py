"""Versioned local data, atomic replacement, and process-scoped locks."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from platformdirs import user_data_path

from .errors import AWMError


def data_home() -> Path:
    override = os.environ.get("AWM_DATA_HOME")
    return (
        Path(override).expanduser().resolve()
        if override
        else user_data_path("awm", appauthor=False)
    )


def read_json(path: Path, default: dict | None = None) -> dict:
    if path.is_symlink():
        raise AWMError(f"State files must not be symlinks: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise AWMError(f"Missing state: {path}") from None
    except (OSError, ValueError) as exc:
        raise AWMError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise AWMError(f"Unsupported or corrupt state: {path}")
    return value


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AWMError(f"Refusing to replace a symlink: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


@contextmanager
def lock(path: Path, *, shared: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AWMError(
                f"Project or registry is busy; retry when its operation finishes: {path}"
            ) from exc
        yield
    finally:
        os.close(fd)


def identity(path: Path) -> list[int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_dir():
        raise AWMError(f"Expected a real directory, not a file or symlink: {path}")
    return [stat.st_dev, stat.st_ino]
