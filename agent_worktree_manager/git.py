"""Git metadata and operations; no workspace naming conventions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import AWMError
from .process import run


@dataclass
class Worktree:
    repo: Path
    path: Path
    branch: str | None
    prunable: bool = False
    locked: bool = False


def git(repo: Path, *args: str, check: bool = False):
    return run(["git", "-C", str(repo), *args], check=check)


def common_dir(repo: Path) -> Path:
    result = git(repo, "rev-parse", "--git-common-dir", check=True)
    return (repo / result.stdout.strip()).resolve()


def worktrees_of(repo: Path, *, include_primary: bool = False) -> list[Worktree]:
    result = git(repo, "worktree", "list", "--porcelain", "-z", check=True)
    records: list[dict] = []
    current: dict = {}
    for field in result.stdout.split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
        else:
            key, _, value = field.partition(" ")
            current[key] = value
    if current:
        records.append(current)
    output = []
    for record in records:
        if "bare" in record:
            continue
        path = Path(record["worktree"])
        if not include_primary and path.resolve() == repo.resolve():
            continue
        branch = record.get("branch")
        output.append(
            Worktree(
                repo,
                path,
                branch.removeprefix("refs/heads/") if branch else None,
                "prunable" in record or not path.exists(),
                "locked" in record,
            )
        )
    return output


def is_dirty(path: Path) -> bool:
    return bool(git(path, "status", "--porcelain", "--untracked-files=all", check=True).stdout)


def is_orphan_worktree_dir(path: Path, repo: Path) -> bool:
    gitfile = path / ".git"
    if path.is_symlink() or not gitfile.is_file() or gitfile.is_symlink():
        return False
    try:
        text = gitfile.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir: "):
            return False
        target = (path / text[8:]).resolve()
        return target.is_relative_to(common_dir(repo) / "worktrees")
    except (OSError, AWMError):
        return False


def check_repo(path: Path) -> None:
    if not path.is_dir() or git(path, "rev-parse", "--show-toplevel").returncode:
        raise AWMError(f"Not a Git checkout: {path}")
    top = Path(git(path, "rev-parse", "--show-toplevel", check=True).stdout.strip()).resolve()
    if top != path.resolve():
        raise AWMError(f"Configure the repository root {top}, not {path}")


def check_new_branch(repo: Path, branch: str, ref: str) -> str:
    git(repo, "check-ref-format", "--branch", branch, check=True)
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise AWMError(f"Branch already exists in {repo}: {branch}")
    return git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", check=True
    ).stdout.strip()
