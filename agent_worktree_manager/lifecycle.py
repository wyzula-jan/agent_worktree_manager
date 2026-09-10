"""Project-owned sandbox lifecycle, used identically by the CLI and TUI."""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from packaging.utils import canonicalize_name

from . import environments as envs
from .config import Project, editable_target, load_project, slug
from .errors import AWMError
from .git import (
    check_new_branch,
    check_repo,
    common_dir,
    git,
    is_dirty,
    is_orphan_worktree_dir,
    worktrees_of,
)
from .registry import entries, protected_paths, registry_lock
from .shell import activation_script
from .storage import identity, lock, read_json, write_json


def read_state(project: Project) -> dict:
    state = read_json(
        project.local / "state.json",
        {"schema_version": 1, "project_id": project.id, "sandboxes": {}},
    )
    if state.get("project_id") != project.id or not isinstance(state.get("sandboxes"), dict):
        raise AWMError(f"Project state identity/schema mismatch: {project.root}")
    try:
        for name, sandbox in state["sandboxes"].items():
            slug(name)
            if not all(
                isinstance(sandbox.get(key), list)
                for key in ("worktrees", "environments", "editables")
            ):
                raise ValueError("resource lists are missing")
            if "error" not in sandbox or (
                sandbox["error"] is not None and not isinstance(sandbox["error"], str)
            ):
                raise ValueError("invalid operation error")
            for link in sandbox["editables"]:
                if (
                    not isinstance(link["name"], str)
                    or not Path(link["path"]).is_absolute()
                    or not isinstance(link["shared"], bool)
                ):
                    raise ValueError("invalid editable link")
            if sandbox["name"] != name or sandbox["status"] not in (
                "creating",
                "ready",
                "imported",
                "deleting",
                "partial",
                "failed",
            ):
                raise ValueError("invalid sandbox name/status")
            for resource in sandbox["worktrees"] + sandbox["environments"]:
                path = Path(resource["path"])
                ident = resource["identity"]
                if not path.is_absolute() or (
                    ident is not None
                    and (
                        not isinstance(ident, list)
                        or len(ident) != 2
                        or not all(isinstance(x, int) for x in ident)
                    )
                ):
                    raise ValueError("invalid resource path/identity")
            for resource in sandbox["environments"]:
                if resource["kind"] not in ("venv", "uv", "conda"):
                    raise ValueError("invalid environment backend")
            for resource in sandbox["worktrees"]:
                if (
                    not Path(resource["repo"]).is_absolute()
                    or not Path(resource["common_dir"]).is_absolute()
                ):
                    raise ValueError("invalid repository identity")
    except (KeyError, TypeError, ValueError) as exc:
        raise AWMError(f"Corrupt sandbox state: {exc}") from exc
    return state


def save_state(project: Project, state: dict) -> None:
    write_json(project.local / "state.json", state)


def active_paths() -> set[Path]:
    return {
        Path(p).resolve()
        for p in (sys.prefix, os.environ.get("VIRTUAL_ENV"), os.environ.get("CONDA_PREFIX"))
        if p
    }


def protection(project: Project, path: Path) -> str | None:
    resolved = path.resolve()
    if path.is_symlink() or resolved != path:
        return "resource path or an ancestor is a symlink"
    for protected in protected_paths(project) | active_paths():
        if resolved == protected or protected.is_relative_to(resolved):
            return f"contains or is a protected checkout/environment: {protected}"
        environment = (
            protected in active_paths() | {project.base}
            or (protected / "pyvenv.cfg").is_file()
            or (protected / "conda-meta").is_dir()
        )
        if environment and resolved.is_relative_to(protected):
            return f"inside a protected environment: {protected}"
    if (path / "conda-meta").is_dir() and (
        (path / "condabin").is_dir() or (path / "pkgs").is_dir()
    ):
        return "conda installation root"
    return None


def assert_resource(project: Project, resource: dict) -> bool:
    path = Path(resource["path"])
    reason = protection(project, path)
    if reason:
        raise AWMError(f"Refusing {path}: {reason}")
    current = identity(path)
    if current is None:
        return False
    if resource["identity"] is None or current != resource["identity"]:
        raise AWMError(f"Directory identity changed or ownership is unproven: {path}")
    return True


def assert_unclaimed(
    project: Project, resources: list[dict], *, except_name: str | None = None
) -> None:
    roots = {project.root: project}
    for entry in entries():
        if Path(entry["root"]).exists():
            other = load_project(Path(entry["root"]))
            roots[other.root] = other
    paths = {Path(r["path"]).resolve() for r in resources}
    if len(paths) != len(resources):
        raise AWMError("An operation contains duplicate resource paths")
    for other in roots.values():
        for name, sandbox in read_state(other)["sandboxes"].items():
            if other.id == project.id and name == except_name:
                continue
            for resource in sandbox["worktrees"] + sandbox["environments"]:
                existing = Path(resource["path"]).resolve()
                if any(
                    p == existing or p.is_relative_to(existing) or existing.is_relative_to(p)
                    for p in paths
                ):
                    raise AWMError(f"Resource already belongs to {other.name}/{name}: {existing}")


def sandbox_record(name: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "status": "creating",
        "created": time.time(),
        "error": None,
        "worktrees": [],
        "environments": [],
        "editables": [],
    }


def worktree_record(alias: str, repo: Path, path: Path, branch: str | None) -> dict:
    return {
        "alias": alias,
        "repo": str(repo),
        "common_dir": str(common_dir(repo)),
        "path": str(path),
        "branch": branch,
        "identity": identity(path),
    }


def selected_repositories(project: Project, aliases: list[str]) -> list[str]:
    if not aliases:
        if len(project.repos) != 1:
            raise AWMError("Select repositories explicitly with --repo ALIAS")
        aliases = list(project.repos)
    if len(set(aliases)) != len(aliases) or any(a not in project.repos for a in aliases):
        raise AWMError("Repository selection contains unknown or duplicate aliases")
    return aliases


def creation_plan(project: Project, name: str, aliases: list[str], ref: str) -> dict:
    slug(name)
    aliases = selected_repositories(project, aliases)
    if name in read_state(project)["sandboxes"]:
        raise AWMError(f"Sandbox already exists: {name}; inspect or delete it first")
    parent = project.local / "sandboxes" / name
    if parent.resolve() != parent:
        raise AWMError(f"Destination has a symlink ancestor: {parent}")
    if parent.exists() or parent.is_symlink():
        raise AWMError(f"Destination already exists: {parent}")
    record = sandbox_record(name)
    record["base"] = str(project.base)
    record["base_snapshot"] = envs.validate_base(project.base, project.backend)
    for alias in aliases:
        repo = project.repos[alias]
        check_repo(repo.path)
        commit = check_new_branch(repo.path, name, ref)
        resource = worktree_record(alias, repo.path, parent / "worktrees" / alias, name)
        resource["commit"] = commit
        record["worktrees"].append(resource)
    record["environments"] = [
        {"path": str(parent / "env"), "kind": project.backend, "identity": None}
    ]
    # Validate all sources before creating any resources.
    selected = {w["alias"]: Path(w["path"]) for w in record["worktrees"]}
    editable_targets: dict[str, set[str]] = {}
    for package in record["base_snapshot"]["packages"]:
        source = envs.editable_path(package)
        if source is None:
            if project.backend != "conda" or not package["conda_owned"]:
                envs.requirement(package)
            continue
        if not source.is_dir():
            raise AWMError(f"Editable source is unavailable: {source}")
        matches = sorted(
            (r for r in project.repos.values() if source.is_relative_to(r.path)),
            key=lambda r: len(r.path.parts),
            reverse=True,
        )
        repo = matches[0] if matches else None
        isolated = bool(repo and repo.alias in selected)
        target = selected[repo.alias] / source.relative_to(repo.path) if isolated else source
        editable_targets[str(target)] = set()
        record["editables"].append(
            {"name": package["name"], "path": str(target), "shared": not isolated}
        )
    for alias, target_root in selected.items():
        for target in project.repos[alias].targets:
            subdir, extras = editable_target(target)
            editable_targets.setdefault(str(target_root / subdir), set()).update(extras)
    record["install_editables"] = {p: sorted(extras) for p, extras in editable_targets.items()}
    assert_unclaimed(project, record["worktrees"] + record["environments"])
    return record


def verify_environment(record: dict, actual: dict) -> list[dict]:
    before = {canonicalize_name(p["name"]): p for p in record["base_snapshot"]["packages"]}
    after = {canonicalize_name(p["name"]): p for p in actual["packages"]}
    if actual["python"] != record["base_snapshot"]["python"]:
        raise AWMError("Sandbox Python version differs from the base")
    requested = {Path(p).resolve(): set(v) for p, v in record["install_editables"].items()}
    found = set()
    extras = {}
    links = []
    isolated_roots = [Path(w["path"]) for w in record["worktrees"]]
    for name, package in after.items():
        path = envs.editable_path(package)
        isolated = path is not None and any(path.is_relative_to(root) for root in isolated_roots)
        if path is not None:
            if path not in requested:
                raise AWMError(f"Unexpected editable destination for {name}: {path}")
            found.add(path)
            extras[name] = requested[path]
            links.append({"name": package["name"], "path": str(path), "shared": not isolated})
        if not isolated and (name not in before or before[name]["version"] != package["version"]):
            raise AWMError(f"Installed dependency drift: {name}")
    if set(before) - set(after):
        raise AWMError(
            f"Packages missing from sandbox: {', '.join(sorted(set(before) - set(after)))}"
        )
    if requested.keys() - found:
        raise AWMError(f"Editable destinations were not installed: {requested.keys() - found}")
    errors = envs.dependency_errors(actual, extras)
    if errors:
        raise AWMError(
            "Sandbox dependencies conflict with the installed base:\n" + "\n".join(errors)
        )
    return links


def create(
    project: Project, name: str, aliases: list[str], ref: str = "HEAD", *, dry_run: bool = False
) -> dict:
    if dry_run:
        return creation_plan(project, name, aliases, ref)
    with registry_lock(), lock(project.local / "operation.lock"):
        record = creation_plan(project, name, aliases, ref)
        state = read_state(project)
        state["sandboxes"][name] = record
        save_state(project, state)
        try:
            for resource in record["worktrees"]:
                path = Path(resource["path"])
                path.mkdir(parents=True)
                resource["identity"] = identity(path)
                save_state(project, state)
                git(
                    Path(resource["repo"]),
                    "worktree",
                    "add",
                    "-b",
                    name,
                    str(path),
                    resource["commit"],
                    check=True,
                )
                resource["branch_created"] = True
                save_state(project, state)
            resource = record["environments"][0]
            target = Path(resource["path"])
            target.mkdir(parents=True)
            resource["identity"] = identity(target)
            save_state(project, state)
            envs.create_environment(project.backend, project.base, target)
            packages = [
                envs.requirement(p)
                for p in record["base_snapshot"]["packages"]
                if envs.editable_path(p) is None
                and (project.backend != "conda" or not p["conda_owned"])
            ]
            envs.install(project.backend, target, packages, [])
            targets = [
                p + (f"[{','.join(extras)}]" if extras else "")
                for p, extras in record["install_editables"].items()
            ]
            envs.install(project.backend, target, [], targets)
            record["editables"] = verify_environment(record, envs.inspect(target))
            if envs.inspect(project.base) != record["base_snapshot"]:
                raise AWMError("Base changed during creation; retry with a stable base")
            record["status"] = "ready"
            save_state(project, state)
            return record
        except (Exception, KeyboardInterrupt) as exc:
            record["status"], record["error"] = "failed", str(exc) or "Interrupted"
            rollback_errors = []
            for resource in reversed(record["environments"] + record["worktrees"]):
                try:
                    if not assert_resource(project, resource):
                        continue
                    path = Path(resource["path"])
                    if "repo" in resource:
                        if (path / ".git").is_file():
                            if is_dirty(path):
                                raise AWMError(f"Preserving changed worktree: {path}")
                            git(Path(resource["repo"]), "worktree", "remove", str(path), check=True)
                        elif not any(path.iterdir()):
                            path.rmdir()
                        else:
                            raise AWMError(f"Preserving unverified partial checkout: {path}")
                        if resource.get("branch_created"):
                            git(
                                Path(resource["repo"]),
                                "branch",
                                "-d",
                                resource["branch"],
                                check=True,
                            )
                            resource["branch_created"] = False
                            resource["branch"] = None
                    else:
                        shutil.rmtree(
                            path
                        )  # this attempt reserved and exclusively owns this directory
                except (AWMError, OSError) as cleanup:
                    rollback_errors.append(str(cleanup))
            record["error"] += "".join(f"\nRollback: {e}" for e in rollback_errors)
            save_state(project, state)
            raise AWMError(record["error"]) from exc


def check_worktree(project: Project, resource: dict, *, force: bool = False) -> str:
    exists = assert_resource(project, resource)
    repo, path = Path(resource["repo"]), Path(resource["path"])
    if str(common_dir(repo)) != resource["common_dir"]:
        raise AWMError(f"Repository identity changed: {repo}")
    registered = next(
        (w for w in worktrees_of(repo, include_primary=True) if w.path.resolve() == path), None
    )
    if registered and registered.locked:
        raise AWMError(f"Worktree is locked; unlock it explicitly in Git first: {path}")
    if not exists:
        return "missing"
    if not is_orphan_worktree_dir(path, repo):
        raise AWMError(f"Not a linked worktree of the recorded repository: {path}")
    if registered is None:
        if not force:
            raise AWMError(f"Orphaned worktree requires --force: {path}")
        return "orphan"
    if registered.branch != resource["branch"]:
        raise AWMError(f"Worktree branch changed; inspect its ownership before deleting: {path}")
    if is_dirty(path) and not force:
        raise AWMError(f"Worktree has uncommitted changes; use --force to discard them: {path}")
    return "registered"


def deletion_plan(
    project: Project,
    names: list[str],
    *,
    force: bool = False,
    keep_env: bool = False,
    keep_worktrees: bool = False,
    environment_paths: set[str] | None = None,
) -> list[dict]:
    if keep_env and keep_worktrees:
        raise AWMError("--keep-env and --keep-worktrees cannot be combined")
    state = read_state(project)
    result = []
    for name in dict.fromkeys(names):
        if name not in state["sandboxes"]:
            raise AWMError(
                f"Sandbox not found: {name}; import existing resources before managing them"
            )
        record = state["sandboxes"][name]
        kept_envs = [
            e
            for e in record["environments"]
            if keep_env or (environment_paths is not None and e["path"] not in environment_paths)
        ]
        for resource in [] if keep_worktrees else record["worktrees"]:
            if any(Path(e["path"]).is_relative_to(Path(resource["path"])) for e in kept_envs):
                raise AWMError(
                    "Cannot keep an environment while deleting the worktree containing it"
                )
            check_worktree(project, resource, force=force)
        for resource in [] if keep_env else record["environments"]:
            if environment_paths is not None and resource["path"] not in environment_paths:
                continue
            if assert_resource(project, resource):
                path = Path(resource["path"])
                if not (path / "pyvenv.cfg").is_file() and not (path / "conda-meta").is_dir():
                    raise AWMError(
                        f"Not a recognized environment; inspect the incomplete resource: {path}"
                    )
                if resource["kind"] == "conda":
                    envs.executable("conda")
        result.append(record)
    return result


def delete(
    project: Project,
    names: list[str],
    *,
    force: bool = False,
    keep_env: bool = False,
    keep_worktrees: bool = False,
    delete_branch: bool = False,
    dry_run: bool = False,
    environment_paths: set[str] | None = None,
) -> list[dict]:
    options = dict(
        force=force,
        keep_env=keep_env,
        keep_worktrees=keep_worktrees,
        environment_paths=environment_paths,
    )
    if dry_run:
        return deletion_plan(project, names, **options)
    with registry_lock(), lock(project.local / "operation.lock"):
        deletion_plan(project, names, **options)  # preflight the entire requested batch
        state = read_state(project)
        removed = []
        for name in dict.fromkeys(names):
            record = state["sandboxes"][name]
            record["status"] = "deleting"
            save_state(project, state)
            try:
                for resource in list([] if keep_worktrees else record["worktrees"]):
                    kind = check_worktree(project, resource, force=force)
                    repo, path = Path(resource["repo"]), Path(resource["path"])
                    if kind == "registered":
                        git(
                            repo,
                            "worktree",
                            "remove",
                            *(["--force"] if force else []),
                            str(path),
                            check=True,
                        )
                    elif kind == "orphan":
                        shutil.rmtree(path)
                    git(repo, "worktree", "prune", check=True)
                    if delete_branch and resource["branch"]:
                        branch = resource["branch"]
                        if (
                            git(
                                repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
                            ).returncode
                            == 0
                        ):
                            git(repo, "branch", "-D" if force else "-d", branch, check=True)
                    record["worktrees"].remove(resource)
                    save_state(project, state)
                # Reaching here proves that none of this sandbox's requested worktree deletions failed.
                for resource in list([] if keep_env else record["environments"]):
                    if environment_paths is not None and resource["path"] not in environment_paths:
                        continue
                    if assert_resource(project, resource):
                        envs.remove_environment(resource["kind"], Path(resource["path"]))
                    record["environments"].remove(resource)
                    save_state(project, state)
                if record["worktrees"] or record["environments"]:
                    record["status"], record["error"] = "partial", None
                else:
                    del state["sandboxes"][name]
                    # Remove only empty managed container directories.
                    parent = project.local / "sandboxes" / name
                    for directory in (
                        (parent / "worktrees", parent) if "base_snapshot" in record else ()
                    ):
                        if (
                            directory.is_dir()
                            and not directory.is_symlink()
                            and not any(directory.iterdir())
                        ):
                            directory.rmdir()
                save_state(project, state)
                removed.append(record)
            except (Exception, KeyboardInterrupt) as exc:
                record["status"], record["error"] = "failed", str(exc) or "Interrupted"
                save_state(project, state)
                raise AWMError(record["error"]) from exc
        return removed


def run_command(
    project: Project,
    name: str,
    args: list[str],
    repo: str | None = None,
    environment: str | None = None,
) -> int:
    if not args:
        raise AWMError("Supply a command after --")
    with lock(project.local / "operation.lock", shared=True):
        target, cwd = execution_target(project, name, repo, environment)
        return envs.run_environment(target["kind"], Path(target["path"]), args, cwd)


def execution_target(
    project: Project,
    name: str,
    repo: str | None,
    environment: str | None,
    *,
    allow_no_worktree: bool = False,
) -> tuple[dict, Path]:
    """Resolve owned resources while the caller holds the shared project lock."""
    record = read_state(project)["sandboxes"].get(name)
    if record is None or record["status"] not in ("ready", "imported", "partial"):
        raise AWMError("Sandbox is absent or not ready; inspect it before running commands")
    targets = [
        e
        for e in record["environments"]
        if environment is None or e["path"] == str(Path(environment).expanduser().resolve())
    ]
    worktrees = [w for w in record["worktrees"] if repo is None or w["alias"] == repo]
    env_only = allow_no_worktree and not record["worktrees"] and repo is None
    if len(targets) != 1 or (len(worktrees) != 1 and not env_only):
        raise AWMError("Select one worktree with --repo and, for mixed imports, one --env PATH")
    if not assert_resource(project, targets[0]) or any(
        not assert_resource(project, w) for w in worktrees
    ):
        raise AWMError("Sandbox resources are missing")
    return targets[0], Path(worktrees[0]["path"]) if worktrees else project.root


def prepare_activation(
    project: Project, name: str, shell: str, repo: str | None = None, environment: str | None = None
) -> str:
    with lock(project.local / "operation.lock", shared=True):
        target, cwd = execution_target(project, name, repo, environment, allow_no_worktree=True)
        return activation_script(target["kind"], Path(target["path"]), cwd, shell)


def project_summary(project: Project) -> dict:
    sandboxes = read_state(project)["sandboxes"]
    states: dict[str, int] = {}
    for sandbox in sandboxes.values():
        status = sandbox["status"]
        if status in ("ready", "imported") and any(
            not Path(r["path"]).exists() for r in sandbox["worktrees"] + sandbox["environments"]
        ):
            status = "missing"
        states[status] = states.get(status, 0) + 1
    return {
        "id": project.id,
        "name": project.name,
        "root": str(project.root),
        "sandboxes": len(sandboxes),
        "states": states,
    }
