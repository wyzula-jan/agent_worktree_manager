"""Regression coverage for the original cleanup behaviors, with explicit ownership."""

import json
import shutil

import pytest
from conftest import git, make_venv

from agent_worktree_manager import cli, environments, migration
from agent_worktree_manager.errors import AWMError


def add_worktree(project, repo, name="t1"):
    path = repo.parent / f"{repo.name}_{name}"
    git(repo, "worktree", "add", "-b", name, str(path), "main")
    migration.import_sandbox(project, name, [f"demo={path}"], [])
    return path


def delete(project, *args):
    return cli.main(["delete", "--root", str(project.root), *args, "--yes"])


def adopt_env(project, name, path, kind="venv"):
    migration.import_sandbox(project, name, [], [f"{kind}={path}"])


def test_list_finds_worktree_sandbox(workspace, capsys):
    project, repo = workspace
    add_worktree(project, repo)
    assert cli.main(["list", "--root", str(project.root)]) == 0
    assert "demo[t1]" in capsys.readouterr().out


def test_delete_clean_worktree_keeps_branch(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    assert delete(project, "t1") == 0
    assert (
        not path.exists() and "t1" in git(repo, "branch", "--format=%(refname:short)").splitlines()
    )


def test_delete_branch_flag(workspace):
    project, repo = workspace
    add_worktree(project, repo)
    assert delete(project, "t1", "--delete-branch") == 0
    assert "t1" not in git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_dirty_worktree_refused_without_force(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    (path / "dirty.txt").write_text("keep")
    assert delete(project, "t1") == 1 and path.exists()
    assert delete(project, "t1", "--force") == 0 and not path.exists()


def test_batch_delete(workspace):
    project, repo = workspace
    paths = [add_worktree(project, repo, n) for n in ("one", "two")]
    assert delete(project, "one", "two") == 0
    assert all(not p.exists() for p in paths)


def test_env_only_sandbox(workspace, tmp_path):
    project, _ = workspace
    path = make_venv(tmp_path / "env")
    adopt_env(project, "envonly", path)
    assert delete(project, "envonly") == 0 and not path.exists()


def test_worktree_only_when_env_missing(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    assert delete(project, "t1") == 0 and not path.exists()


def test_keep_env_and_keep_worktrees(workspace, tmp_path):
    project, repo = workspace
    path = add_worktree(project, repo)
    env = make_venv(tmp_path / "env")
    adopt_env(project, "t1", env)
    assert delete(project, "t1", "--keep-worktrees") == 0
    assert path.exists() and not env.exists()
    assert delete(project, "t1", "--keep-env") == 0 and not path.exists()


def test_nothing_found(workspace):
    assert delete(workspace[0], "ghost") == 1


def test_full_dir_name_hint(workspace, capsys):
    project, repo = workspace
    path = add_worktree(project, repo)
    assert delete(project, path.name) == 1
    assert "Sandbox not found" in capsys.readouterr().err
    assert path.exists()


def test_dry_run_deletes_nothing(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    before = (project.local / "state.json").read_bytes()
    assert delete(project, "t1", "--dry-run") == 0
    assert path.exists() and (project.local / "state.json").read_bytes() == before


def test_stale_registration_pruned(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    shutil.rmtree(path)
    assert delete(project, "t1") == 0
    assert str(path) not in git(repo, "worktree", "list", "--porcelain")


def test_orphan_dir_needs_force(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    shutil.rmtree(repo / ".git" / "worktrees" / path.name)
    assert delete(project, "t1") == 1 and path.exists()
    assert delete(project, "t1", "--force") == 0 and not path.exists()


def test_plain_data_dir_never_touched(workspace):
    project, repo = workspace
    path = repo.parent / "demo_deploy"
    path.mkdir()
    (path / "artifact").write_text("keep")
    with pytest.raises(AWMError, match="proven linked worktree"):
        migration.import_sandbox(project, "deploy", [f"demo={path}"], [])
    assert delete(project, "deploy", "--force") == 1 and path.exists()


def test_primary_checkout_never_touched(workspace):
    project, repo = workspace
    with pytest.raises(AWMError):
        migration.import_sandbox(project, "main", [f"demo={repo}"], [])
    assert (repo / ".git").is_dir()


def test_uv_env_only_sandbox(workspace, tmp_path):
    project, _ = workspace
    path = make_venv(tmp_path / "uv-env")
    adopt_env(project, "uvonly", path, "uv")
    assert delete(project, "uvonly") == 0 and not path.exists()


def test_uv_venv_and_worktree_both_removed(workspace, tmp_path):
    project, repo = workspace
    path = add_worktree(project, repo)
    env = make_venv(tmp_path / "uv-env")
    adopt_env(project, "t1", env, "uv")
    assert delete(project, "t1") == 0
    assert not path.exists() and not env.exists()


def test_deletes_uv_and_legacy_conda_env_together(workspace, tmp_path, monkeypatch):
    project, _ = workspace
    uv = make_venv(tmp_path / "uv-env")
    conda = make_venv(tmp_path / "conda-env")
    (conda / "conda-meta").mkdir()
    migration.import_sandbox(project, "both", [], [f"uv={uv}", f"conda={conda}"])
    calls = []
    real_remove = environments.remove_environment
    monkeypatch.setattr(environments, "executable", lambda k: "conda" if k == "conda" else "uv")

    def remove(kind, path):
        calls.append((kind, path))
        real_remove("venv", path)

    monkeypatch.setattr(environments, "remove_environment", remove)
    assert delete(project, "both") == 0
    assert {kind for kind, _ in calls} == {"uv", "conda"}
    assert not uv.exists() and not conda.exists()


def test_uv_keep_env(workspace, tmp_path):
    project, repo = workspace
    path = add_worktree(project, repo)
    env = make_venv(tmp_path / "uv-env")
    adopt_env(project, "t1", env, "uv")
    assert delete(project, "t1", "--keep-env") == 0
    assert env.exists() and not path.exists()


def test_base_env_itself_is_never_a_sandbox(workspace):
    project, _ = workspace
    with pytest.raises(AWMError, match="protected"):
        adopt_env(project, "base", project.base)
    assert project.base.exists()


def test_remove_uv_env_refuses_outside_venv_home(workspace, tmp_path):
    project, _ = workspace
    external = make_venv(tmp_path / "external")
    assert delete(project, "external", "--force") == 1
    assert external.exists()  # location alone never proves ownership


def test_remove_uv_env_refuses_non_venv(workspace, tmp_path):
    project, _ = workspace
    plain = tmp_path / "data"
    plain.mkdir()
    with pytest.raises(AWMError, match="recognized"):
        adopt_env(project, "data", plain)
    assert plain.exists()


def test_list_reports_env_kind(workspace, tmp_path, capsys):
    project, _ = workspace
    adopt_env(project, "env", make_venv(tmp_path / "uv-env"), "uv")
    assert cli.main(["list", "--root", str(project.root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["projects"][0]["sandboxes"][0]["environments"][0]["kind"] == "uv"


def declare_version(source, version, name="demo_pkg"):
    pyproject = source / "pyproject.toml"
    pyproject.write_text(
        f'{pyproject.read_text()}\n[project]\nname = "{name}"\nversion = "{version}"\n'
    )


def test_doctor_reports_stale_editable_metadata(workspace, install_package, capsys):
    project, repo = workspace
    install_package(project.base, repo)
    declare_version(repo, "2.0")
    assert cli.main(["doctor", "--root", str(project.root)]) == 0
    out = capsys.readouterr().out
    assert "Stale editable metadata: demo_pkg records 1.0" in out
    assert "declares 2.0" in out


def test_doctor_is_quiet_when_editable_metadata_matches(workspace, install_package, capsys):
    project, repo = workspace
    install_package(project.base, repo)
    declare_version(repo, "1.0")
    assert cli.main(["doctor", "--root", str(project.root)]) == 0
    assert "Stale editable metadata" not in capsys.readouterr().out
