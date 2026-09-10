"""A per-user index; unregistering never removes project data."""

from __future__ import annotations

from pathlib import Path

from .config import Project, discover_root, load_project
from .errors import AWMError
from .storage import data_home, lock, read_json, write_json


def registry_path() -> Path:
    return data_home() / "projects.json"


def registry_lock():
    return lock(data_home() / "registry.lock")


def entries() -> list[dict]:
    value = read_json(registry_path(), {"schema_version": 1, "projects": []})
    projects = value.get("projects")
    if not isinstance(projects, list) or any(
        not isinstance(p, dict)
        or not all(isinstance(p.get(k), str) for k in ("id", "name", "root", "base"))
        for p in projects
    ):
        raise AWMError("Corrupt project registry")
    if len({p["id"] for p in projects}) != len(projects):
        raise AWMError("Duplicate project identifiers in registry")
    return projects


def register(project: Project) -> None:
    with registry_lock():
        projects = entries()
        for item in projects:
            if (
                item["id"] == project.id
                and Path(item["root"]) != project.root
                and Path(item["root"]).exists()
            ):
                raise AWMError(
                    "This project ID belongs to another existing directory; do not copy .awm between projects"
                )
        projects = [
            p for p in projects if p["id"] != project.id and Path(p["root"]) != project.root
        ]
        projects.append(
            {
                "id": project.id,
                "name": project.name,
                "root": str(project.root),
                "base": str(project.base),
            }
        )
        write_json(registry_path(), {"schema_version": 1, "projects": projects})


def find(selector: str) -> dict:
    matches = [p for p in entries() if p["id"] == selector or p["name"] == selector]
    if len(matches) != 1:
        raise AWMError(
            f"Unknown or ambiguous project '{selector}'; use an ID from 'awm projects list'"
        )
    return matches[0]


def unregister(selector: str) -> None:
    with registry_lock():
        project_id = find(selector)["id"]
        write_json(
            registry_path(),
            {"schema_version": 1, "projects": [p for p in entries() if p["id"] != project_id]},
        )


def select(*, root: str | None = None, project: str | None = None) -> Project:
    if root and project:
        raise AWMError("Use either --root or --project, not both")
    if project:
        entry = find(project)
        result = load_project(Path(entry["root"]))
        if result.id != entry["id"]:
            raise AWMError("Registered project identity changed; register the directory again")
        return result
    return load_project(Path(root) if root else discover_root())


def protected_paths(current: Project) -> set[Path]:
    result = {current.base, *(r.path for r in current.repos.values())}
    for entry in entries():
        result.add(Path(entry["base"]).resolve())
        try:
            project = load_project(Path(entry["root"]))
        except AWMError:
            continue
        result.add(project.base)
        result.update(repo.path for repo in project.repos.values())
    return result
