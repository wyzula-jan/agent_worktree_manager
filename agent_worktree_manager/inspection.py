"""Read-only inspection and background size work shared by terminal views."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import environments, lifecycle
from .config import Project
from .environments import Env
from .errors import AWMError
from .git import Worktree, git
from .process import run

SIZES: dict[str, int | None] = {}


def _du_kb(path: str | Path) -> int | None:
    try:
        r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=30)
        return int(r.stdout.split()[0]) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        return None


class SizeWorker:
    def __init__(self) -> None:
        self._queue: list[str] = []
        self._priority: str | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, paths) -> None:
        with self._lock:
            for p in paths:
                p = str(p)
                if p not in SIZES and p not in self._queue:
                    self._queue.append(p)
            if self._queue and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def prioritize(self, path: str | Path | None) -> None:
        with self._lock:
            self._priority = str(path) if path else None

    def _next(self) -> str | None:
        with self._lock:
            if self._priority in self._queue:
                p = self._priority
                self._queue.remove(p)
                return p
            if self._queue:
                return self._queue.pop(0)
            self._thread = None
            return None

    def _run(self) -> None:
        while (p := self._next()) is not None:
            SIZES[p] = _du_kb(p)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def human_age(ts: float) -> str:
    delta = max(0, time.time() - ts)
    for unit, sec in (("y", 31536000), ("mo", 2592000), ("w", 604800), ("d", 86400), ("h", 3600)):
        if delta >= sec:
            return f"{int(delta // sec)}{unit}"
    return "<1h"


def human_size(kb: int | None) -> str:
    if kb is None:
        return "…"
    if kb >= 1 << 20:
        return f"{kb / (1 << 20):.1f}G"
    if kb >= 1 << 10:
        return f"{kb / (1 << 10):.0f}M"
    return f"{kb}K"


def _matches(text: str, query: str) -> bool:
    """grep-style: every whitespace-separated term must occur (case-insensitive)."""
    t = text.lower()
    return all(term in t for term in query.lower().split())


# ---------------------------------------------------------------------------
# Sandbox metadata
# ---------------------------------------------------------------------------
@dataclass
class WtInfo:
    wt: Worktree
    dirty: int = 0
    last_age: str = "?"
    last_subject: str = ""
    unpushed: str = ""

    @property
    def size_kb(self) -> int | None:
        return SIZES.get(str(self.wt.path))


@dataclass
class Item:
    name: str
    envs: list = field(default_factory=list)  # list[Env]
    wts: list[WtInfo] = field(default_factory=list)
    selected: bool = False
    status: str = "imported"
    error: str | None = None
    editables: list[dict] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return any(w.dirty for w in self.wts)

    @property
    def env_size_kb(self) -> int | None:
        sizes = [SIZES.get(e.path) for e in self.envs]
        return sum(filter(None, sizes)) if any(s is not None for s in sizes) else None

    @property
    def total_kb(self) -> int:
        return sum(filter(None, [self.env_size_kb, *(w.size_kb for w in self.wts)]))

    @property
    def sizes_pending(self) -> bool:
        return any(e.path not in SIZES for e in self.envs) or any(
            str(w.wt.path) not in SIZES for w in self.wts if not w.wt.prunable
        )

    @property
    def filter_text(self) -> str:
        return " ".join(
            [self.name, self.status, *(e.name for e in self.envs)]
            + [f"{w.wt.repo.name} {w.wt.branch or ''}" for w in self.wts]
        )


def wt_info(wt: Worktree) -> WtInfo:
    info = WtInfo(wt=wt)
    if wt.prunable:
        info.last_subject = "(directory missing)"
        return info
    r = git(wt.path, "status", "--porcelain")
    if r.returncode == 0:
        info.dirty = len([line for line in r.stdout.splitlines() if line.strip()])
    r = git(wt.path, "log", "-1", "--format=%ct%x00%s")
    if r.returncode == 0 and r.stdout.strip():
        ts, _, subject = r.stdout.strip().partition("\x00")
        try:
            info.last_age = human_age(int(ts))
        except ValueError:
            pass
        info.last_subject = subject
    r = git(wt.path, "rev-list", "--count", "@{upstream}..HEAD")
    if r.returncode == 0:
        n = int(r.stdout.strip() or 0)
        info.unpushed = f"{n} unpushed" if n else "pushed"
    else:
        info.unpushed = "no upstream"
    return info


@dataclass
class Pkg:
    name: str
    version: str
    editable: str | None  # source dir for editable installs

    @property
    def filter_text(self) -> str:
        return f"{self.name} {self.version} {self.editable or ''}"


@dataclass
class EnvItem:
    env: Env
    python: str = "?"
    sandbox: str | None = None  # owning sandbox, when registered
    protected: str | None = None  # reason it cannot be deleted, else None
    changed: float | None = None  # last modification timestamp
    selected: bool = False
    pkgs: list[Pkg] | None = None  # loaded on demand
    pkgs_error: str | None = None
    pkgs_loading: bool = False

    @property
    def name(self) -> str:
        return self.env.name

    @property
    def size_kb(self) -> int | None:
        return SIZES.get(self.env.path)

    @property
    def interpreter(self) -> str:
        return str(Path(self.env.path) / "bin" / "python")

    @property
    def filter_text(self) -> str:
        return f"{self.name} {self.env.kind} {self.python} {self.sandbox or ''}"


def python_version_of(path: str | Path) -> str:
    p = Path(path)
    cfg = p / "pyvenv.cfg"
    if cfg.is_file():
        try:
            for line in cfg.read_text(errors="replace").splitlines():
                key, _, val = line.partition("=")
                if key.strip() in ("version_info", "version") and val.strip():
                    return val.strip()
        except OSError:
            pass
    meta = p / "conda-meta"
    if meta.is_dir():
        for f in meta.glob("python-[0-9]*.json"):
            m = re.match(r"python-(\d+\.\d+(?:\.\d+)?)-", f.name)
            if m:
                return m.group(1)
    return "?" if (p / "bin" / "python").exists() else "-"


def env_changed_ts(path: str | Path) -> float | None:
    p = Path(path)
    candidates = [p, p / "pyvenv.cfg", p / "conda-meta" / "history"]
    candidates += list((p / "lib").glob("python*/site-packages")) if (p / "lib").is_dir() else []
    stamps = []
    for c in candidates:
        try:
            stamps.append(c.stat().st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else None


def gather(project: Project) -> list[Item]:
    items = []
    for record in lifecycle.read_state(project)["sandboxes"].values():
        item = Item(
            record["name"],
            status=record["status"],
            error=record["error"],
            editables=record["editables"],
        )
        item.envs = [Env(record["name"], e["path"], e["kind"]) for e in record["environments"]]
        for resource in record["worktrees"]:
            wt = Worktree(
                Path(resource["repo"]),
                Path(resource["path"]),
                resource["branch"],
                not Path(resource["path"]).exists(),
            )
            try:
                item.wts.append(wt_info(wt))
            except AWMError as exc:
                item.wts.append(WtInfo(wt, last_subject=f"metadata unavailable: {exc}"))
        items.append(item)
    return sorted(items, key=lambda i: i.name)


def make_env_item(project: Project, env: Env, sandbox: str | None = None) -> EnvItem:
    return EnvItem(
        env,
        python=python_version_of(env.path),
        sandbox=sandbox,
        changed=env_changed_ts(env.path),
        protected=lifecycle.protection(project, Path(env.path))
        or (None if sandbox else "not owned; import before deleting"),
    )


def gather_envs(project: Project) -> list[EnvItem]:
    found = {
        str(project.base): make_env_item(
            project, Env(project.base.name, str(project.base), project.backend)
        )
    }
    for record in lifecycle.read_state(project)["sandboxes"].values():
        for resource in record["environments"]:
            env = Env(record["name"], resource["path"], resource["kind"])
            item = make_env_item(project, env, record["name"])
            try:
                if not lifecycle.assert_resource(project, resource):
                    item.protected = "directory missing"
            except AWMError as exc:
                item.protected = str(exc)
            found[env.path] = item
    # Show nearby environments for inspection, never infer their ownership.
    if project.base.parent.is_dir():
        for path in project.base.parent.iterdir():
            if (path / "pyvenv.cfg").is_file() and not path.is_symlink():
                found.setdefault(
                    str(path), make_env_item(project, Env(path.name, str(path), "venv"))
                )
    if project.backend == "conda":
        for env in environments.conda_environments():
            found.setdefault(env.path, make_env_item(project, env))
    return sorted(found.values(), key=lambda i: (i.env.kind, i.name.lower()))


def list_packages(python: str, timeout: float = 120) -> tuple[list[Pkg], str | None]:
    if not os.access(python, os.X_OK):
        return [], "no python interpreter in this env"
    try:
        output = run([python, "-I", "-B", "-c", environments.INSPECT], timeout=timeout)
        data = json.loads(output.stdout)
        packages = {}
        for d in data["packages"]:
            path = environments.editable_path(d)
            packages[d["name"].lower()] = Pkg(d["name"], d["version"], str(path) if path else None)
        return sorted(packages.values(), key=lambda p: p.name.lower()), None
    except (AWMError, ValueError, KeyError) as exc:
        return [], str(exc)
