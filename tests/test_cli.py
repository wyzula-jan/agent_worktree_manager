import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worktree_manager import cli


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _branches(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fake PSI root with one repo 'demo' and no conda."""
    (tmp_path / cli.ROOT_MARKER).write_text("")
    repo = tmp_path / "demo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    monkeypatch.setattr(cli, "find_conda", lambda: None)
    monkeypatch.setattr(cli, "conda_envs", lambda conda: {})
    return tmp_path, repo


def _add_worktree(root: Path, repo: Path, name: str) -> Path:
    wt = root / f"{repo.name}_{name}"
    _git("worktree", "add", "-b", name, str(wt), "main", cwd=repo)
    return wt


def test_list_finds_worktree_sandbox(workspace, capsys):
    root, repo = workspace
    _add_worktree(root, repo, "t1")
    assert cli.main(["list", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "demo[t1]" in out


def test_delete_clean_worktree_keeps_branch(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 0
    assert not wt.exists()
    assert "t1" in _branches(repo)  # committed work survives by default


def test_delete_branch_flag(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--delete-branch"]) == 0
    assert not wt.exists()
    assert "t1" not in _branches(repo)


def test_dirty_worktree_refused_without_force(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    (wt / "dirty.txt").write_text("uncommitted\n")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 1
    assert wt.exists()
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--force"]) == 0
    assert not wt.exists()


def test_batch_delete(workspace):
    root, repo = workspace
    wt1 = _add_worktree(root, repo, "t1")
    wt2 = _add_worktree(root, repo, "t2")
    assert cli.main(["delete", "t1", "t2", "--root", str(root), "--yes"]) == 0
    assert not wt1.exists() and not wt2.exists()


def test_env_only_sandbox(workspace, monkeypatch):
    root, _ = workspace
    removed = []
    monkeypatch.setattr(cli, "find_conda", lambda: "/fake/conda")
    monkeypatch.setattr(
        cli, "conda_envs", lambda conda: {"bec_312_envonly": "/fake/envs/bec_312_envonly"}
    )
    monkeypatch.setattr(
        cli,
        "remove_env",
        lambda conda, name: (removed.append(name), subprocess.CompletedProcess([], 0, "", ""))[1],
    )
    assert cli.main(["delete", "envonly", "--root", str(root), "--yes"]) == 0
    assert removed == ["bec_312_envonly"]


def test_worktree_only_when_env_missing(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    # no env exists (conda_envs -> {}): the worktree half is still removed
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 0
    assert not wt.exists()


def test_keep_env_and_keep_worktrees(workspace, monkeypatch):
    root, repo = workspace
    removed = []
    monkeypatch.setattr(cli, "find_conda", lambda: "/fake/conda")
    monkeypatch.setattr(cli, "conda_envs", lambda conda: {"bec_312_t1": "/fake/envs/bec_312_t1"})
    monkeypatch.setattr(
        cli,
        "remove_env",
        lambda conda, name: (removed.append(name), subprocess.CompletedProcess([], 0, "", ""))[1],
    )
    wt = _add_worktree(root, repo, "t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--keep-worktrees"]) == 0
    assert wt.exists() and removed == ["bec_312_t1"]
    monkeypatch.setattr(cli, "conda_envs", lambda conda: {})
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--keep-env"]) == 0
    assert not wt.exists()


def test_nothing_found(workspace, capsys):
    root, _ = workspace
    assert cli.main(["delete", "ghost", "--root", str(root), "--yes"]) == 1
    assert "nothing found" in capsys.readouterr().err


def test_full_dir_name_hint(workspace, capsys):
    root, repo = workspace
    _add_worktree(root, repo, "t1")
    cli.main(["delete", "demo_t1", "--root", str(root), "--yes"])
    assert "did you mean 't1'" in capsys.readouterr().err


def test_dry_run_deletes_nothing(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--dry-run"]) == 0
    assert wt.exists()


def test_stale_registration_pruned(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    shutil.rmtree(wt)  # user already rm -rf'd the dir by hand
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 0
    porcelain = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "demo_t1" not in porcelain


def test_orphan_dir_needs_force(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    # simulate git losing track of the worktree while the dir stays behind
    shutil.rmtree(repo / ".git" / "worktrees" / "demo_t1")
    (repo / ".git" / "worktrees").mkdir(exist_ok=True)
    (repo / ".git" / "worktrees" / "demo_t1").mkdir()
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True, capture_output=True)
    shutil.rmtree(repo / ".git" / "worktrees" / "demo_t1", ignore_errors=True)
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 1
    assert wt.exists()
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--force"]) == 0
    assert not wt.exists()


def test_plain_data_dir_never_touched(workspace):
    root, repo = workspace
    decoy = root / "demo_deploy"  # matches naming scheme but is not a worktree
    decoy.mkdir()
    (decoy / "artifact.bin").write_text("keep me\n")
    assert cli.main(["delete", "deploy", "--root", str(root), "--yes", "--force"]) == 1
    assert decoy.exists() and (decoy / "artifact.bin").exists()


def test_primary_checkout_never_touched(workspace, tmp_path):
    root, repo = workspace
    # a second primary repo whose name matches "<repo>_<name>" for repo 'demo'
    other = root / "demo_tools"
    other.mkdir()
    _git("init", "-b", "main", cwd=other)
    assert cli.main(["delete", "tools", "--root", str(root), "--yes", "--force"]) == 1
    assert (other / ".git").is_dir()


# ---------------------------------------------------------------------------
# uv venv sandboxes (and conda -> uv migration)
# ---------------------------------------------------------------------------
def _make_venv(venv_home: Path, name: str) -> Path:
    """A directory that looks like a uv venv to remove_uv_env()."""
    venv = venv_home / name
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    return venv


def test_uv_env_only_sandbox(workspace):
    root, _ = workspace
    venv = _make_venv(root / cli.VENV_DIRNAME, "bec_base_uvonly")
    assert cli.main(["delete", "uvonly", "--root", str(root), "--yes"]) == 0
    assert not venv.exists()


def test_uv_venv_and_worktree_both_removed(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    venv = _make_venv(root / cli.VENV_DIRNAME, "bec_base_t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes"]) == 0
    assert not wt.exists() and not venv.exists()


def test_deletes_uv_and_legacy_conda_env_together(workspace, monkeypatch):
    """Half-migrated sandbox: both envs go in one call."""
    root, _ = workspace
    venv = _make_venv(root / cli.VENV_DIRNAME, "bec_base_both")
    removed = []
    monkeypatch.setattr(cli, "find_conda", lambda: "/fake/conda")
    monkeypatch.setattr(
        cli, "conda_envs", lambda conda: {"bec_312_both": "/fake/envs/bec_312_both"}
    )
    monkeypatch.setattr(
        cli,
        "remove_env",
        lambda conda, name: (removed.append(name), subprocess.CompletedProcess([], 0, "", ""))[1],
    )
    assert cli.main(["delete", "both", "--root", str(root), "--yes"]) == 0
    assert removed == ["bec_312_both"]
    assert not venv.exists()


def test_uv_keep_env(workspace):
    root, repo = workspace
    wt = _add_worktree(root, repo, "t1")
    venv = _make_venv(root / cli.VENV_DIRNAME, "bec_base_t1")
    assert cli.main(["delete", "t1", "--root", str(root), "--yes", "--keep-env"]) == 0
    assert venv.exists() and not wt.exists()


def test_base_env_itself_is_never_a_sandbox(workspace):
    """`bec_base` has no `_<name>` suffix, so it is never picked up."""
    root, _ = workspace
    base = _make_venv(root / cli.VENV_DIRNAME, "bec_base")
    assert cli.main(["delete", "base", "--root", str(root), "--yes"]) == 1
    assert base.exists()


def test_remove_uv_env_refuses_outside_venv_home(tmp_path):
    outside = _make_venv(tmp_path / "elsewhere", "victim")
    with pytest.raises(ValueError, match="not under"):
        cli.remove_uv_env(str(outside), tmp_path / ".venvs")
    assert outside.exists()


def test_remove_uv_env_refuses_non_venv(tmp_path):
    venv_home = tmp_path / ".venvs"
    plain = venv_home / "not-a-venv"
    plain.mkdir(parents=True)
    (plain / "important.txt").write_text("keep me\n")
    with pytest.raises(ValueError, match="does not look like a venv"):
        cli.remove_uv_env(str(plain), venv_home)
    assert plain.exists()


def test_list_reports_env_kind(workspace, capsys):
    root, repo = workspace
    _add_worktree(root, repo, "t1")
    _make_venv(root / cli.VENV_DIRNAME, "bec_base_t1")
    assert cli.main(["list", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "uv" in out and "t1" in out
