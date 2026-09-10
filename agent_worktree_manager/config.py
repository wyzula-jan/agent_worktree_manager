"""Portable workspace configuration and per-machine settings."""

from __future__ import annotations

import json
import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .errors import AWMError
from .git import check_repo
from .storage import atomic_text, lock

CONFIG_NAME = "awm.toml"
BACKENDS = ("venv", "uv", "conda")


def slug(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise AWMError(
            "Names must be 1–80 letters, digits, dots, underscores or dashes, starting with a letter or digit"
        )
    return value


def read_toml(path: Path) -> dict:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AWMError(f"Cannot read {path}: {exc}") from exc
    if value.get("schema_version") != 1:
        raise AWMError(f"Unsupported configuration version: {path}")
    return value


def editable_target(target: str) -> tuple[str, tuple[str, ...]]:
    subdir, separator, suffix = target.partition("[")
    if not subdir or Path(subdir).is_absolute() or ".." in Path(subdir).parts:
        raise ValueError(f"editable target must stay inside its repository: {target}")
    if not separator:
        return subdir, ()
    requirement = Requirement("editable-target[" + suffix)
    if not target.endswith("]") or requirement.marker or requirement.specifier or requirement.url:
        raise ValueError(f"invalid editable target extras: {target}")
    return subdir, tuple(sorted(canonicalize_name(extra) for extra in requirement.extras))


@dataclass(frozen=True)
class Repository:
    alias: str
    path: Path
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Project:
    root: Path
    id: str
    name: str
    backend: str
    base: Path
    repos: dict[str, Repository]

    @property
    def local(self) -> Path:
        path = self.root / ".awm"
        if path.is_symlink():
            raise AWMError(f"Project state directory must not be a symlink: {path}")
        return path


def load_project(root: Path) -> Project:
    root = root.expanduser().resolve()
    if root.name == CONFIG_NAME and root.is_file():
        root = root.parent
    shared = read_toml(root / CONFIG_NAME)
    if (root / ".awm").is_symlink():
        raise AWMError("Project state directory must not be a symlink")
    local = read_toml(root / ".awm" / "local.toml")
    return parse_project(root, shared, local)


def parse_project(root: Path, shared: dict, local: dict) -> Project:
    try:
        project_id = str(uuid.UUID(local["project_id"]))
        name = shared["name"]
        environment = local["environment"]
        backend, base = environment["backend"], environment["base"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or backend not in BACKENDS
            or not isinstance(base, str)
        ):
            raise ValueError("invalid project name or environment")
        repos = {}
        overrides = local.get("repositories", {})
        if not isinstance(overrides, dict) or overrides.keys() - shared["repositories"].keys():
            raise ValueError("local repository overrides must use configured aliases")
        for alias, spec in shared["repositories"].items():
            slug(alias)
            spec = {**spec, **overrides.get(alias, {})}
            targets = spec.get("targets", [])
            if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
                raise ValueError(f"invalid editable targets for {alias}")
            for target in targets:
                editable_target(target)
            repos[alias] = Repository(
                alias, (root / Path(spec["path"]).expanduser()).resolve(), tuple(targets)
            )
        if not repos or len({r.path for r in repos.values()}) != len(repos):
            raise ValueError("configure at least one repository, with distinct checkout paths")
        return Project(
            root, project_id, name, backend, (root / Path(base).expanduser()).resolve(), repos
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AWMError(f"Invalid project configuration at {root}: {exc}") from exc


def discover_root(cwd: Path | None = None) -> Path:
    path = (cwd or Path.cwd()).resolve()
    for candidate in (path, *path.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise AWMError(
        "No project selected; use --project, --root, or run inside an initialized project"
    )


def initialize(
    root: Path,
    base: Path,
    backend: str,
    name: str | None,
    repositories: list[str],
    *,
    dry_run: bool = False,
) -> Project | None:
    root, base = root.expanduser().resolve(), base.expanduser().resolve()
    if not root.is_dir():
        raise AWMError(f"Project directory does not exist: {root}")
    if not (base / "bin" / "python").is_file():
        raise AWMError(f"Base has no Python interpreter: {base}")
    if backend == "conda" and not (base / "conda-meta").is_dir():
        raise AWMError(f"Not a conda environment: {base}")
    if backend != "conda" and not (base / "pyvenv.cfg").is_file():
        raise AWMError(f"Select a dedicated venv base, or use --backend conda: {base}")
    if (root / ".awm").is_symlink():
        raise AWMError("Project state directory must not be a symlink")
    shared_path = root / CONFIG_NAME
    if shared_path.exists():
        if repositories or name:
            raise AWMError(
                "Configuration already exists; edit awm.toml to change its name/repositories"
            )
        shared = read_toml(shared_path)
        text = None
    else:
        entries = repositories or ["repo=."]
        text = f"schema_version = 1\nname = {json.dumps(name or root.name)}\n"
        seen = set()
        for entry in entries:
            alias, sep, value = entry.partition("=")
            if not sep or not value or slug(alias) in seen:
                raise AWMError("Repositories use unique ALIAS=PATH entries")
            seen.add(alias)
            path = (root / Path(value).expanduser()).resolve()
            check_repo(path)
            text += f"\n[repositories.{json.dumps(alias)}]\npath = {json.dumps(os.path.relpath(path, root))}\ntargets = []\n"
    if (root / ".awm" / "local.toml").exists():
        raise AWMError("Project is already initialized; edit .awm/local.toml to change its base")
    local_text = (
        f'schema_version = 1\nproject_id = "{uuid.uuid4()}"\n\n[environment]\n'
        f"backend = {json.dumps(backend)}\nbase = {json.dumps(str(base))}\n"
    )
    candidate = parse_project(
        root, tomllib.loads(text) if text is not None else shared, tomllib.loads(local_text)
    )
    for repo in candidate.repos.values():
        check_repo(repo.path)
    if dry_run:
        return None
    with lock(root / ".awm" / "operation.lock"):
        if (root / ".awm" / "local.toml").exists():
            raise AWMError("Project was initialized by another process")
        atomic_text(root / ".awm" / ".gitignore", "*\n")
        if text is not None:
            atomic_text(shared_path, text)
        atomic_text(root / ".awm" / "local.toml", local_text)
    return load_project(root)
