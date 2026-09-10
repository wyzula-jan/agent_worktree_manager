"""The installed awm entry point. Mutations belong to shared services."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from . import __version__, environments, lifecycle, migration, registry
from .config import BACKENDS, initialize, load_project
from .errors import AWMError
from .git import check_repo, is_dirty


def log(message: str) -> None:
    print(f"[awm] {message}")


def err(message: str) -> None:
    print(f"[awm] ERROR: {message}", file=sys.stderr)


def selected(opts):
    return registry.select(root=getattr(opts, "root", None), project=getattr(opts, "project", None))


def confirmation(message: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise AWMError("Non-interactive deletion/import requires --yes; use --dry-run to preview")
    try:
        return input(message + " [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def print_plan(project, record: dict) -> None:
    log(f"{project.name}/{record['name']} ({record['status']})")
    for worktree in record["worktrees"]:
        print(
            f"  worktree {worktree['alias']}[{worktree['branch'] or 'detached'}]  {worktree['path']}"
        )
    for env in record["environments"]:
        print(f"  env {env['kind']}  {env['path']}")
    for editable in record["editables"]:
        print(
            f"  editable {editable['name']} -> {editable['path']} ({'shared source' if editable['shared'] else 'isolated source'})"
        )
    if record.get("error"):
        print(f"  error: {record['error']}")


def project_overview() -> list[dict]:
    output = []
    for entry in registry.entries():
        try:
            project = load_project(Path(entry["root"]))
            if project.id != entry["id"]:
                raise AWMError("Project identity changed; re-register this location")
            output.append(lifecycle.project_summary(project))
        except (AWMError, OSError) as exc:
            output.append({**entry, "unavailable": str(exc)})
    return output


def cmd_projects(opts) -> int:
    if opts.action == "add":
        project = load_project(Path(opts.path))
        if not opts.dry_run:
            registry.register(project)
        log(f"{'Would register' if opts.dry_run else 'Registered'} {project.name}: {project.id}")
    elif opts.action == "remove":
        entry = registry.find(opts.selector)
        if not opts.dry_run:
            registry.unregister(opts.selector)
        log(
            f"{'Would unregister' if opts.dry_run else 'Unregistered'} {entry['name']}; project resources are preserved"
        )
    else:
        items = project_overview()
        if opts.json:
            print(json.dumps({"schema_version": 1, "projects": items}, indent=2))
        for item in [] if opts.json else items:
            print(
                f"{item['id']}  {item['name']}  {item.get('states', item.get('unavailable'))}  {item['root']}"
            )
    return 0


def cmd_init(opts) -> int:
    if getattr(opts, "project", None):
        raise AWMError("init takes --root, not --project")
    project = initialize(
        Path(getattr(opts, "root", None) or "."),
        Path(opts.base),
        opts.backend,
        opts.name,
        opts.repo,
        dry_run=opts.dry_run,
    )
    if project:
        registry.register(project)
        log(f"Initialized {project.name}: {project.id}")
    else:
        log("Initialization preflight passed; no files written")
    return 0


def cmd_create(opts) -> int:
    project = selected(opts)
    record = lifecycle.create(project, opts.name, opts.repo, opts.ref, dry_run=True)
    print_plan(project, record)
    if opts.dry_run:
        log("Dry run; no resources created")
    else:
        log("Creating worktrees and reproducing the installed base…")
        print_plan(project, lifecycle.create(project, opts.name, opts.repo, opts.ref))
    return 0


def cmd_import(opts) -> int:
    project = selected(opts)
    kwargs = dict(
        legacy=opts.legacy,
        venv_home=opts.venv_home,
        base_env=opts.base_env,
        conda_base_env=opts.conda_base_env,
    )
    record = migration.import_sandbox(
        project, opts.name, opts.worktree, opts.env, dry_run=True, **kwargs
    )
    print_plan(project, record)
    if opts.dry_run:
        log("Dry run; ownership records unchanged")
    elif confirmation("Adopt these resources for management by awm?", opts.yes):
        migration.import_sandbox(project, opts.name, opts.worktree, opts.env, **kwargs)
        log("Import complete")
    else:
        return 1
    return 0


def cmd_delete(opts) -> int:
    project = selected(opts)
    kwargs = dict(
        force=opts.force,
        keep_env=opts.keep_env,
        keep_worktrees=opts.keep_worktrees,
        delete_branch=opts.delete_branch,
    )
    records = lifecycle.delete(project, opts.names, dry_run=True, **kwargs)
    for record in records:
        print_plan(project, record)
    log(
        f"Delete worktrees: {not opts.keep_worktrees}; delete environments: {not opts.keep_env}; delete branches: {opts.delete_branch}; force: {opts.force}"
    )
    if opts.dry_run:
        log("Dry run; nothing deleted")
    elif confirmation(f"Delete selected resources in {project.name}?", opts.yes):
        lifecycle.delete(project, opts.names, **kwargs)
        log("Deletion complete")
    else:
        return 1
    return 0


def list_payload(project) -> dict:
    records = []
    for record in lifecycle.read_state(project)["sandboxes"].values():
        summary = {
            k: record[k]
            for k in ("name", "status", "error", "worktrees", "environments", "editables")
        }
        summary["worktrees"] = []
        for resource in record["worktrees"]:
            path = Path(resource["path"])
            try:
                dirty = is_dirty(path) if path.exists() else None
            except AWMError:
                dirty = None
            summary["worktrees"].append({**resource, "missing": not path.exists(), "dirty": dirty})
        summary["environments"] = [
            {**e, "missing": not Path(e["path"]).exists()} for e in record["environments"]
        ]
        records.append(summary)
    return {"id": project.id, "name": project.name, "root": str(project.root), "sandboxes": records}


def cmd_list(opts) -> int:
    projects = []
    if opts.all:
        if getattr(opts, "root", None) or getattr(opts, "project", None):
            raise AWMError("--all cannot be combined with a project selector")
        for entry in registry.entries():
            try:
                projects.append(list_payload(registry.select(project=entry["id"])))
            except (AWMError, OSError) as exc:
                projects.append({**entry, "unavailable": str(exc)})
    else:
        projects = [list_payload(selected(opts))]
    if opts.json:
        print(json.dumps({"schema_version": 1, "projects": projects}, indent=2))
    else:
        for project in projects:
            print(f"{project['name']} ({project['id']})  {project['root']}")
            if "unavailable" in project:
                print(f"  unavailable: {project['unavailable']}")
            for record in project.get("sandboxes", []):
                kinds = ", ".join(e["kind"] for e in record["environments"]) or "-"
                worktrees = (
                    ", ".join(
                        f"{w['alias']}[{w['branch'] or 'detached'}]{'*' if w['dirty'] else ''}{' (missing)' if w['missing'] else ''}"
                        for w in record["worktrees"]
                    )
                    or "-"
                )
                print(f"  {record['name']}  {record['status']}  {kinds}  {worktrees}")
                if record["error"]:
                    print(f"    {record['error']}")
    return 0


def cmd_doctor(opts) -> int:
    project = selected(opts)
    for repo in project.repos.values():
        check_repo(repo.path)
    base = environments.validate_base(project.base, project.backend)
    lifecycle.read_state(project)
    for package in base["packages"]:
        path = environments.editable_path(package)
        if path and not path.exists():
            raise AWMError(f"Missing editable source: {path}")
    log(
        f"{project.name}: Git checkouts and {project.backend} base are valid; Python {base['python']}, {len(base['packages'])} packages"
    )
    return 0


def cmd_run(opts) -> int:
    command = opts.args[1:] if opts.args[:1] == ["--"] else opts.args
    return lifecycle.run_command(selected(opts), opts.name, command, opts.repo, opts.env)


def cmd_skill(opts) -> int:
    from .agent_skill import bundled_files, install_skill

    if opts.install:
        log(f"Skill installed at {install_skill(Path(opts.install))}")
    else:
        print(bundled_files()["SKILL.md"].decode("utf-8"), end="")
    return 0


def cmd_shell_init(opts) -> int:
    from .shell import shell_init

    print(shell_init(opts.shell), end="")
    return 0


def cmd_ui(opts) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AWMError("Interactive mode requires a TTY; use 'awm projects list' or 'awm list'")
    from .tui import run_ui

    return run_ui(opts)


def build_parser() -> argparse.ArgumentParser:
    selectors = argparse.ArgumentParser(add_help=False)
    group = selectors.add_mutually_exclusive_group()
    group.add_argument("--root", default=argparse.SUPPRESS, help="project root containing awm.toml")
    group.add_argument("--project", default=argparse.SUPPRESS, help="registered project name or ID")
    parser = argparse.ArgumentParser(
        prog="awm",
        description="Manage project worktrees and isolated Python environments",
        parents=[selectors],
    )
    parser.add_argument("--version", action="version", version=f"awm {__version__}")
    parser.set_defaults(func=cmd_ui, envs=False)
    commands = parser.add_subparsers(dest="command")
    shell = commands.add_parser("shell-init", help="print current-shell integration for eval")
    shell.add_argument("shell", choices=("bash", "zsh"))
    shell.set_defaults(func=cmd_shell_init)
    skill = commands.add_parser("skill", help="print or install the bundled coding-agent skill")
    skill.add_argument("--install", metavar="PATH", help="copy the skill into this skill directory")
    skill.set_defaults(func=cmd_skill)
    init = commands.add_parser("init", parents=[selectors], help="configure and register a project")
    init.add_argument("--base", required=True, help="existing base environment directory")
    init.add_argument("--backend", choices=BACKENDS, default="venv")
    init.add_argument("--name")
    init.add_argument("--repo", action="append", default=[], metavar="ALIAS=PATH")
    init.add_argument("--dry-run", action="store_true")
    init.set_defaults(func=cmd_init)
    create = commands.add_parser(
        "create", parents=[selectors], help="create a sandbox from the installed base"
    )
    create.add_argument("name")
    create.add_argument("--repo", action="append", default=[])
    create.add_argument("--ref", default="HEAD")
    create.add_argument("-n", "--dry-run", action="store_true")
    create.set_defaults(func=cmd_create)
    imp = commands.add_parser(
        "import", parents=[selectors], help="explicitly adopt existing worktrees/environments"
    )
    imp.add_argument("name")
    imp.add_argument("--worktree", action="append", default=[], metavar="ALIAS=PATH")
    imp.add_argument("--env", action="append", default=[], metavar="BACKEND=PATH")
    imp.add_argument("--legacy", action="store_true")
    for flag in ("venv-home", "base-env", "conda-base-env"):
        imp.add_argument(f"--{flag}")
    imp.add_argument("-n", "--dry-run", action="store_true")
    imp.add_argument("-y", "--yes", action="store_true")
    imp.set_defaults(func=cmd_import)
    delete = commands.add_parser(
        "delete", parents=[selectors], help="delete owned sandbox resources"
    )
    delete.add_argument("names", nargs="+")
    for short, long in (("-n", "--dry-run"), ("-y", "--yes"), ("-f", "--force")):
        delete.add_argument(short, long, action="store_true")
    for flag in ("keep-env", "keep-worktrees", "delete-branch"):
        delete.add_argument(f"--{flag}", action="store_true")
    delete.set_defaults(func=cmd_delete)
    ls = commands.add_parser("list", parents=[selectors], help="list owned sandboxes")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(func=cmd_list)
    doctor = commands.add_parser(
        "doctor", parents=[selectors], help="check project prerequisites without changing them"
    )
    doctor.set_defaults(func=cmd_doctor)
    run = commands.add_parser(
        "run", parents=[selectors], help="run a command in a sandbox; options precede NAME"
    )
    run.add_argument("--repo")
    run.add_argument("--env")
    run.add_argument("name")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)
    for command in ("ui", "envs"):
        ui = commands.add_parser(command, parents=[selectors])
        ui.add_argument("--envs", action="store_true", default=command == "envs")
        ui.set_defaults(func=cmd_ui)
    projects = commands.add_parser("projects", help="register or browse projects")
    actions = projects.add_subparsers(dest="action", required=True)
    for action in ("add", "remove", "list"):
        p = actions.add_parser(action)
        if action != "list":
            p.add_argument("path" if action == "add" else "selector")
            p.add_argument("--dry-run", action="store_true")
        else:
            p.add_argument("--json", action="store_true")
        p.set_defaults(func=cmd_projects)
    return parser


def main(argv: list[str] | None = None) -> int:
    previous = {}

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, interrupted)
    try:
        opts = build_parser().parse_args(argv)
        return opts.func(opts)
    except (AWMError, OSError, ValueError) as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        err("Interrupted")
        return 130
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
