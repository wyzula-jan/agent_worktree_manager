"""Explicit adoption of existing resources; legacy naming lives only here."""

from __future__ import annotations

from pathlib import Path

from . import environments as envs
from .config import Project, slug
from .errors import AWMError
from .git import is_orphan_worktree_dir, worktrees_of
from .lifecycle import (
    assert_resource,
    assert_unclaimed,
    read_state,
    sandbox_record,
    save_state,
    worktree_record,
)
from .registry import registry_lock
from .storage import identity, lock


def plan_import(
    project: Project,
    name: str,
    worktrees: list[str],
    environments: list[str],
    *,
    legacy: bool = False,
    venv_home: str | None = None,
    base_env: str | None = None,
    conda_base_env: str | None = None,
) -> dict:
    slug(name)
    requested_wts, requested_envs = list(worktrees), list(environments)
    if legacy:
        for alias, repo in project.repos.items():
            expected = repo.path.name + "_" + name
            found = [w.path for w in worktrees_of(repo.path) if w.path.name == expected]
            orphan = repo.path.parent / expected
            if (
                orphan not in found
                and orphan.is_dir()
                and is_orphan_worktree_dir(orphan, repo.path)
            ):
                found.append(orphan)
            requested_wts.extend(f"{alias}={path}" for path in found)
        if bool(venv_home) != bool(base_env):
            raise AWMError("Legacy venv discovery requires both --venv-home and --base-env")
        if venv_home and base_env:
            path = Path(venv_home).expanduser().absolute() / f"{base_env}_{name}"
            if path.exists():
                requested_envs.append(f"uv={path}")
        if conda_base_env:
            matches = [e for e in envs.conda_environments() if e.name == f"{conda_base_env}_{name}"]
            if len(matches) > 1:
                raise AWMError("Legacy conda name is ambiguous; use --env conda=PATH explicitly")
            requested_envs.extend(f"conda={e.path}" for e in matches)
    elif venv_home or base_env or conda_base_env:
        raise AWMError("Legacy naming options require --legacy")
    record = sandbox_record(name)
    record["status"] = "imported"
    for entry in requested_wts:
        alias, sep, raw = entry.partition("=")
        if not sep or alias not in project.repos:
            raise AWMError("Worktrees use a configured ALIAS=PATH")
        path, repo = Path(raw).expanduser().absolute(), project.repos[alias].path
        if path.resolve() != path:
            raise AWMError(f"Import a canonical path without symlinks: {path}")
        registered = next((w for w in worktrees_of(repo) if w.path.resolve() == path), None)
        if path.exists() and not is_orphan_worktree_dir(path, repo):
            raise AWMError(f"Not a proven linked worktree: {path}")
        if not registered and not is_orphan_worktree_dir(path, repo):
            raise AWMError(f"Not a proven linked worktree: {path}")
        resource = worktree_record(alias, repo, path, registered.branch if registered else None)
        assert_resource(project, resource)
        record["worktrees"].append(resource)
    for entry in requested_envs:
        kind, sep, raw = entry.partition("=")
        if not sep or kind not in ("venv", "uv", "conda"):
            raise AWMError("Environments use venv=PATH, uv=PATH, or conda=PATH")
        path = Path(raw).expanduser().absolute()
        marker = "conda-meta" if kind == "conda" else "pyvenv.cfg"
        if not (path / marker).exists() or not (path / "bin" / "python").exists():
            raise AWMError(f"Not a recognized {kind} environment: {path}")
        resource = {"path": str(path), "kind": kind, "identity": identity(path)}
        assert_resource(project, resource)
        record["environments"].append(resource)
    resources = record["worktrees"] + record["environments"]
    if not resources:
        raise AWMError(f"No resources found for '{name}'; specify --worktree or --env")
    assert_unclaimed(project, resources, except_name=name)
    existing = read_state(project)["sandboxes"].get(name)
    if existing:
        for key in ("worktrees", "environments"):
            by_path = {r["path"]: r for r in existing[key]}
            for resource in record[key]:
                old = by_path.get(resource["path"])
                if old and any(old[k] != resource[k] for k in resource):
                    raise AWMError(
                        f"Existing ownership differs; refusing to re-adopt {resource['path']}"
                    )
                by_path[resource["path"]] = old or resource
            existing[key] = list(by_path.values())
        return existing
    return record


def import_sandbox(
    project: Project,
    name: str,
    worktrees: list[str],
    environments: list[str],
    *,
    dry_run: bool = False,
    **legacy_options,
) -> dict:
    if dry_run:
        return plan_import(project, name, worktrees, environments, **legacy_options)
    with registry_lock(), lock(project.local / "operation.lock"):
        record = plan_import(project, name, worktrees, environments, **legacy_options)
        state = read_state(project)
        state["sandboxes"][name] = record
        save_state(project, state)
        return record
