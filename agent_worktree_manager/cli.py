"""awm — agent worktree manager.

Cleanup counterpart to ``clone_worktree_env.sh``: deletes the git worktrees
(``<repo>_<name>``) and the cloned conda env (``<base_env>_<name>``) that make
up a background-task sandbox. Works when only one of the two still exists, and
accepts several sandbox names at once.

Safety model:
  * worktrees with uncommitted changes are refused unless --force
  * branches are kept by default (committed work survives); --delete-branch
    removes them (-d, or -D with --force)
  * directories are only rm -rf'd when they are provably orphaned worktree
    dirs (a ``.git`` *file* pointing into ``<repo>/.git/worktrees/``), and
    only with --force
  * the base env and the primary repo checkouts can never be targeted
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_ENV = "bec_312"
ROOT_MARKER = "clone_worktree_env.sh"
NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def log(msg: str) -> None:
    print(f"{_c('1;34', '[awm]')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('1;33', '[awm]')} {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_c('1;31', '[awm] ERROR:')} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
@dataclass
class Worktree:
    repo: Path  # primary checkout the worktree is registered in
    path: Path  # worktree directory
    branch: str | None  # checked-out branch (None if detached)
    prunable: bool  # registered but directory gone


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def worktrees_of(repo: Path) -> list[Worktree]:
    """Linked worktrees registered in *repo* (the primary checkout excluded)."""
    r = git(repo, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return []
    entries: list[dict] = []
    cur: dict = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val if val else True
    if cur:
        entries.append(cur)

    out = []
    for e in entries:
        path = Path(e["worktree"])
        if path.resolve() == repo.resolve():
            continue  # the primary checkout itself
        branch = e.get("branch")
        out.append(
            Worktree(
                repo=repo,
                path=path,
                branch=branch.removeprefix("refs/heads/") if isinstance(branch, str) else None,
                prunable="prunable" in e or not path.exists(),
            )
        )
    return out


def is_dirty(wt_path: Path) -> bool:
    r = git(wt_path, "status", "--porcelain")
    return bool(r.stdout.strip()) if r.returncode == 0 else False


def is_orphan_worktree_dir(d: Path, repo: Path) -> bool:
    """True if *d* is a leftover worktree dir of *repo* that git no longer knows.

    Requires a ``.git`` *file* whose gitdir points into ``<repo>/.git/worktrees/``
    so plain data directories that happen to match the naming scheme (and the
    primary checkouts, which have a ``.git`` directory) are never touched.
    """
    gitfile = d / ".git"
    if not gitfile.is_file():
        return False
    try:
        content = gitfile.read_text()
    except OSError:
        return False
    m = re.search(r"^gitdir:\s*(.+)$", content, re.MULTILINE)
    if not m:
        return False
    gitdir = Path(m.group(1).strip())
    if not gitdir.is_absolute():
        gitdir = (d / gitdir).resolve()
    try:
        gitdir.relative_to(repo.resolve() / ".git" / "worktrees")
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Conda helpers
# ---------------------------------------------------------------------------
def find_conda() -> str | None:
    exe = os.environ.get("CONDA_EXE", "")
    if exe and os.access(exe, os.X_OK):
        return exe
    exe = shutil.which("conda")
    if exe:
        return exe
    home = Path.home()
    for base in (
        home / "miniforge3",
        home / "miniconda3",
        home / "anaconda3",
        home / "mambaforge",
        Path("/opt/conda"),
        Path("/opt/miniconda3"),
    ):
        for sub in ("condabin/conda", "bin/conda"):
            cand = base / sub
            if os.access(cand, os.X_OK):
                return str(cand)
    return None


def conda_envs(conda: str) -> dict[str, str]:
    """Mapping of env name -> env path."""
    r = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True)
    if r.returncode != 0:
        warn(f"'conda env list' failed: {r.stderr.strip()}")
        return {}
    return {Path(p).name: p for p in json.loads(r.stdout).get("envs", [])}


def remove_env(conda: str, env_name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [conda, "env", "remove", "--yes", "--name", env_name], capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------
def default_root() -> Path:
    for var in ("AWM_ROOT", "PSI_ROOT"):
        val = os.environ.get(var)
        if val:
            return Path(val).expanduser().resolve()
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if (cand / ROOT_MARKER).exists():
            return cand
    for cand in Path(__file__).resolve().parents:
        if (cand / ROOT_MARKER).exists():
            return cand
    return cwd


def find_repos(root: Path) -> list[Path]:
    """Primary checkouts: subdirs with a .git *directory* (worktrees have a file)."""
    if not root.is_dir():
        return []
    return sorted(
        (d for d in root.iterdir() if d.is_dir() and (d / ".git").is_dir()), key=lambda p: p.name
    )


@dataclass
class Sandbox:
    name: str
    sanitized: str
    env_name: str
    worktrees: list[Worktree] = field(default_factory=list)  # live, dir exists
    stale: list[Worktree] = field(default_factory=list)  # registered, dir gone
    orphans: list[tuple[Path, Path]] = field(default_factory=list)  # (repo, dir)
    env_path: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.worktrees or self.stale or self.orphans or self.env_path)


def resolve_sandbox(
    root: Path,
    repo_worktrees: dict[Path, list[Worktree]],
    envs: dict[str, str],
    base_env: str,
    name: str,
) -> Sandbox:
    sanitized = name.replace("/", "-")
    sb = Sandbox(name=name, sanitized=sanitized, env_name=f"{base_env}_{sanitized}")
    for repo, wts in repo_worktrees.items():
        expected = f"{repo.name}_{sanitized}"
        registered = False
        for wt in wts:
            if wt.path.name == expected:
                registered = True
                (sb.stale if wt.prunable else sb.worktrees).append(wt)
        d = root / expected
        if not registered and d.is_dir() and is_orphan_worktree_dir(d, repo):
            sb.orphans.append((repo, d))
    sb.env_path = envs.get(sb.env_name)
    return sb


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def print_plan(sb: Sandbox, opts: argparse.Namespace) -> None:
    log(f"sandbox '{sb.name}':")
    skip_wt = "  (kept: --keep-worktrees)" if opts.keep_worktrees else ""
    for wt in sb.worktrees:
        dirty = _c("1;33", "  [dirty]") if is_dirty(wt.path) else ""
        branch = wt.branch or "(detached)"
        print(
            f"    worktree  {wt.path.name}  (repo: {wt.repo.name}, branch: {branch}){dirty}{skip_wt}"
        )
    for wt in sb.stale:
        print(
            f"    worktree  {wt.path.name}  (repo: {wt.repo.name}) — stale registration, dir gone{skip_wt}"
        )
    for repo, d in sb.orphans:
        print(f"    orphan    {d.name}  (unregistered worktree dir of {repo.name}){skip_wt}")
    if sb.env_path:
        kept = "  (kept: --keep-env)" if opts.keep_env else ""
        print(f"    env       {sb.env_name}  ({sb.env_path}){kept}")
    else:
        print(f"    env       {sb.env_name} — not found, skipping")
    if not (sb.worktrees or sb.stale or sb.orphans):
        print("    worktrees — none found")


def delete_branch(wt: Worktree, force: bool) -> None:
    r = git(wt.repo, "branch", "-D" if force else "-d", wt.branch)
    if r.returncode == 0:
        log(f"[{wt.repo.name}] deleted branch '{wt.branch}'")
    else:
        warn(
            f"[{wt.repo.name}] kept branch '{wt.branch}': {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'git refused'}"
        )


def delete_sandbox(sb: Sandbox, conda: str | None, opts: argparse.Namespace) -> list[str]:
    errors: list[str] = []

    if not opts.keep_worktrees:
        touched_repos = set()
        for wt in sb.worktrees:
            if is_dirty(wt.path) and not opts.force:
                errors.append(
                    f"[{wt.repo.name}] '{wt.path.name}' has uncommitted changes — re-run with --force to discard"
                )
                continue
            args = ["worktree", "remove"]
            if opts.force:
                args += ["--force", "--force"]  # twice: also unlocks locked worktrees
            r = git(wt.repo, *args, str(wt.path))
            if r.returncode != 0:
                errors.append(f"[{wt.repo.name}] worktree remove failed: {r.stderr.strip()}")
                continue
            log(f"[{wt.repo.name}] removed worktree {wt.path.name}")
            touched_repos.add(wt.repo)
            if opts.delete_branch and wt.branch:
                delete_branch(wt, opts.force)

        for wt in sb.stale:
            touched_repos.add(wt.repo)
            log(f"[{wt.repo.name}] pruning stale registration of {wt.path.name}")
            if opts.delete_branch and wt.branch:
                delete_branch(wt, opts.force)

        for repo in touched_repos:
            git(repo, "worktree", "prune")

        for repo, d in sb.orphans:
            if not opts.force:
                errors.append(
                    f"[{repo.name}] '{d.name}' is an orphaned worktree dir — re-run with --force to delete it"
                )
                continue
            shutil.rmtree(d)
            git(repo, "worktree", "prune")
            log(f"[{repo.name}] deleted orphaned worktree dir {d.name}")

    if sb.env_path and not opts.keep_env:
        if conda is None:
            errors.append(f"cannot remove env '{sb.env_name}': conda executable not found")
        else:
            log(f"removing conda env '{sb.env_name}'…")
            r = remove_env(conda, sb.env_name)
            if r.returncode != 0:
                errors.append(f"conda env remove '{sb.env_name}' failed: {r.stderr.strip()}")
            else:
                log(f"removed conda env '{sb.env_name}'")

    return errors


def hint_for_unknown(name: str, repos: list[Path]) -> str | None:
    for repo in sorted(repos, key=lambda p: len(p.name), reverse=True):
        prefix = f"{repo.name}_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return f"'{name}' looks like a worktree dir of '{repo.name}' — did you mean '{name[len(prefix):]}'?"
    return None


def cmd_delete(opts: argparse.Namespace) -> int:
    root = Path(opts.root).expanduser().resolve() if opts.root else default_root()
    repos = find_repos(root)
    if not repos:
        err(f"no git repos found under {root} (use --root to point at the workspace)")
        return 2

    for name in opts.names:
        if not NAME_RE.match(name):
            err(f"invalid name '{name}' (use letters, digits, . _ - /)")
            return 2

    conda = find_conda()
    if conda is None:
        warn("conda not found — env cleanup will be skipped (set CONDA_EXE to fix)")
    envs = conda_envs(conda) if conda else {}
    repo_worktrees = {repo: worktrees_of(repo) for repo in repos}

    log(f"workspace root: {root}  (base env: {opts.base_env})")
    plans: list[Sandbox] = []
    missing = 0
    for name in opts.names:
        sb = resolve_sandbox(root, repo_worktrees, envs, opts.base_env, name)
        if not sb.found:
            missing += 1
            warn(f"nothing found for '{name}' (no worktrees, no env '{sb.env_name}')")
            hint = hint_for_unknown(name, repos)
            if hint:
                warn(f"  {hint}")
            continue
        print_plan(sb, opts)
        plans.append(sb)

    if not plans:
        return 1

    if opts.dry_run:
        log("dry run — nothing deleted")
        return 1 if missing else 0

    if not opts.yes:
        if not sys.stdin.isatty():
            err("stdin is not a TTY — re-run with --yes to confirm non-interactively")
            return 2
        try:
            answer = input(f"Delete the {len(plans)} sandbox(es) above? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            log("aborted")
            return 1

    all_errors: list[str] = []
    for sb in plans:
        errors = delete_sandbox(sb, conda, opts)
        if errors:
            for e in errors:
                err(f"[{sb.name}] {e}")
            all_errors.extend(errors)
        else:
            log(f"sandbox '{sb.name}' fully removed")

    if not opts.delete_branch and any(sb.worktrees for sb in plans):
        log("branches were kept (use --delete-branch to remove them too)")

    return 1 if (all_errors or missing) else 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def cmd_list(opts: argparse.Namespace) -> int:
    root = Path(opts.root).expanduser().resolve() if opts.root else default_root()
    repos = find_repos(root)
    if not repos:
        err(f"no git repos found under {root} (use --root to point at the workspace)")
        return 2

    sandboxes: dict[str, dict] = {}
    for repo in repos:
        prefix = f"{repo.name}_"
        for wt in worktrees_of(repo):
            if wt.path.name.startswith(prefix) and len(wt.path.name) > len(prefix):
                name = wt.path.name[len(prefix) :]
                sandboxes.setdefault(name, {"worktrees": [], "env": None})["worktrees"].append(wt)

    conda = find_conda()
    envs = conda_envs(conda) if conda else {}
    env_prefix = f"{opts.base_env}_"
    for env_name in envs:
        if env_name.startswith(env_prefix) and len(env_name) > len(env_prefix):
            name = env_name[len(env_prefix) :]
            sandboxes.setdefault(name, {"worktrees": [], "env": None})["env"] = env_name

    if opts.json:
        payload = {
            name: {
                "env": sb["env"],
                "worktrees": [
                    {
                        "repo": wt.repo.name,
                        "path": str(wt.path),
                        "branch": wt.branch,
                        "missing": wt.prunable,
                        "dirty": is_dirty(wt.path) if not wt.prunable else False,
                    }
                    for wt in sb["worktrees"]
                ],
            }
            for name, sb in sorted(sandboxes.items())
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not sandboxes:
        log(f"no sandboxes found under {root}")
        return 0

    log(f"workspace root: {root}  (base env: {opts.base_env})")
    name_w = max(len(n) for n in sandboxes) + 2
    env_w = max((len(sb["env"] or "-") for sb in sandboxes.values()), default=1) + 2
    print(f"  {'NAME':<{name_w}}{'ENV':<{env_w}}WORKTREES")
    for name, sb in sorted(sandboxes.items()):
        parts = []
        for wt in sb["worktrees"]:
            marker = " (missing)" if wt.prunable else ("*" if is_dirty(wt.path) else "")
            parts.append(f"{wt.repo.name}[{wt.branch or 'detached'}]{marker}")
        print(f"  {name:<{name_w}}{(sb['env'] or '-'):<{env_w}}{', '.join(parts) or '-'}")
    print("  (* = uncommitted changes)")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        help="workspace root holding the repos (default: $AWM_ROOT/$PSI_ROOT, else the "
        f"nearest dir containing {ROOT_MARKER})",
    )
    common.add_argument(
        "--base-env",
        default=os.environ.get("BASE_ENV", DEFAULT_BASE_ENV),
        help=f"base conda env the sandbox envs were cloned from (default: {DEFAULT_BASE_ENV})",
    )

    parser = argparse.ArgumentParser(
        prog="awm",
        description="Manage (delete/list) agent sandboxes created by clone_worktree_env.sh.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_del = sub.add_parser(
        "delete",
        parents=[common],
        help="delete sandboxes: their <repo>_<name> worktrees and <base_env>_<name> conda env",
    )
    p_del.add_argument("names", nargs="+", metavar="NAME", help="sandbox name(s), e.g. fix-async")
    p_del.add_argument("-n", "--dry-run", action="store_true", help="show what would be deleted")
    p_del.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="discard uncommitted changes, delete orphaned dirs, use -D for branches",
    )
    p_del.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p_del.add_argument("--keep-env", action="store_true", help="delete only the worktrees")
    p_del.add_argument("--keep-worktrees", action="store_true", help="delete only the conda env")
    p_del.add_argument(
        "--delete-branch",
        action="store_true",
        help="also delete each worktree's branch (git branch -d; -D with --force)",
    )
    p_del.set_defaults(func=cmd_delete)

    p_list = sub.add_parser("list", parents=[common], help="list existing sandboxes")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=cmd_list)

    p_ui = sub.add_parser(
        "ui",
        parents=[common],
        help="interactive picker: check/uncheck sandboxes to delete (default)",
    )
    p_ui.set_defaults(func=cmd_ui)

    return parser


def cmd_ui(opts: argparse.Namespace) -> int:
    from agent_worktree_manager.tui import cmd_ui as run

    return run(opts)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["ui"]  # bare `awm` opens the interactive picker
    opts = build_parser().parse_args(argv)
    return opts.func(opts)


if __name__ == "__main__":
    sys.exit(main())
