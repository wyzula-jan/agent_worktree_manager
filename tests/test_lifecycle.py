import shutil
from pathlib import Path

import pytest
from conftest import git, make_venv
from test_cli import add_worktree, adopt_env

from agent_worktree_manager import environments, lifecycle, migration
from agent_worktree_manager.config import load_project
from agent_worktree_manager.errors import AWMError
from agent_worktree_manager.storage import lock


@pytest.mark.parametrize("source_version", ["1.0", "0.0"])
def test_inspect_ignores_editable_source_metadata(workspace, install_package, source_version):
    project, repo = workspace
    install_package(project.base, repo)
    before = environments.inspect(project.base)
    metadata = repo / "demo_pkg.egg-info" / "PKG-INFO"
    metadata.parent.mkdir()
    contents = f"Metadata-Version: 2.1\nName: demo_pkg\nVersion: {source_version}\n"
    metadata.write_text(contents)
    assert environments.inspect(project.base) == before
    assert metadata.read_text() == contents


def test_inspect_rejects_duplicate_installed_metadata(workspace, install_package):
    project, repo = workspace
    install_package(project.base, repo)
    snapshot = environments.inspect(project.base)
    metadata = Path(snapshot["site_packages"]) / "demo_pkg-0.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Metadata-Version: 2.1\nName: demo_pkg\nVersion: 0.0\n")
    with pytest.raises(AWMError, match="duplicate installed distributions"):
        environments.inspect(project.base)


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["venv", "uv"])
def test_create_import_run_delete_preserves_base(make_workspace, install_package, backend, capfd):
    if backend == "uv" and not shutil.which("uv"):
        pytest.skip("uv not installed")
    project, repo = make_workspace(backend=backend)
    install_package(project.base, repo)
    before = environments.inspect(project.base)
    base_files = {
        str(p.relative_to(project.base)): p.read_bytes()
        for p in project.base.rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    record = lifecycle.create(project, "feature", [])
    assert record["status"] == "ready"
    target = Path(record["worktrees"][0]["path"])
    assert record["editables"] == [dict(name="demo_pkg", path=str(target), shared=False)]
    assert lifecycle.run_command(project, "feature", ["demo_pkg-where"]) == 0
    assert str(target / "demo_pkg") in capfd.readouterr().out
    (target / "demo_pkg/__init__.py").write_text('VALUE = "sandbox"\n')
    assert (
        lifecycle.run_command(
            project,
            "feature",
            ["python", "-c", "import demo_pkg; assert demo_pkg.VALUE == 'sandbox'"],
        )
        == 0
    )
    assert environments.inspect(project.base) == before
    assert {
        str(p.relative_to(project.base)): p.read_bytes()
        for p in project.base.rglob("*")
        if p.is_file() and not p.is_symlink()
    } == base_files
    with pytest.raises(AWMError, match="uncommitted"):
        lifecycle.delete(project, ["feature"])
    assert Path(record["environments"][0]["path"]).exists()
    lifecycle.delete(project, ["feature"], force=True)
    assert project.base.exists() and not target.exists()
    assert "feature" in git(repo, "branch", "--format=%(refname:short)").splitlines()


@pytest.mark.integration
def test_pinned_dependency_and_shared_editable(workspace, install_package, tmp_path):
    project, repo = workspace
    install_package(project.base, repo)
    shared = install_package(project.base, tmp_path / "shared source", "shared_pkg")
    install_package(project.base, tmp_path / "third party", "dependency_pkg", editable=False)
    record = lifecycle.create(project, "task", [])
    actual = environments.inspect(Path(record["environments"][0]["path"]))
    assert {p["name"]: p["version"] for p in actual["packages"]}["dependency_pkg"] == "1.0"
    assert {e["name"]: e for e in record["editables"]}["shared_pkg"] == dict(
        name="shared_pkg", path=str(shared), shared=True
    )
    lifecycle.delete(project, ["task"])
    assert shared.exists()


@pytest.mark.integration
def test_multiple_editable_subpackages_and_repository_selection(workspace, install_package):
    project, repo = workspace
    install_package(project.base, repo / "first", "first_pkg")
    install_package(project.base, repo / "second", "second_pkg")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "subpackages")
    record = lifecycle.create(project, "task", ["demo"])
    assert {Path(e["path"]).name for e in record["editables"]} == {"first", "second"}
    lifecycle.delete(project, ["task"])


def test_default_ref_is_current_head_and_no_branch_reuse(workspace):
    project, repo = workspace
    git(repo, "switch", "-c", "other")
    (repo / "other").write_text("head")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "other")
    plan = lifecycle.create(project, "task", [], dry_run=True)
    assert plan["worktrees"][0]["commit"] == git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "task")
    with pytest.raises(AWMError, match="already exists"):
        lifecycle.create(project, "task", [])
    assert not (project.local / "state.json").exists()


def test_create_dry_run_writes_nothing(workspace):
    project, _ = workspace
    before = sorted(str(p) for p in project.root.rglob("*"))
    lifecycle.create(project, "task", [], dry_run=True)
    assert sorted(str(p) for p in project.root.rglob("*")) == before


@pytest.mark.parametrize("exception", [AWMError("artifact unavailable"), KeyboardInterrupt()])
def test_failure_rolls_back_owned_resources_and_records_failure(workspace, monkeypatch, exception):
    project, repo = workspace

    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(environments, "create_environment", fail)
    with pytest.raises(AWMError):
        lifecycle.create(project, "task", [])
    record = lifecycle.read_state(project)["sandboxes"]["task"]
    assert record["status"] == "failed" and record["error"]
    assert not any(Path(r["path"]).exists() for r in record["worktrees"] + record["environments"])
    assert project.base.exists()
    lifecycle.delete(project, ["task"])
    assert not lifecycle.read_state(project)["sandboxes"]


def test_rollback_preserves_user_changes(workspace, monkeypatch):
    project, repo = workspace

    def fail(*args):
        path = project.local / "sandboxes/task/worktrees/demo"
        (path / "user-work").write_text("keep")
        raise AWMError("failed")

    monkeypatch.setattr(environments, "create_environment", fail)
    with pytest.raises(AWMError, match="Preserving changed"):
        lifecycle.create(project, "task", [])
    assert (project.local / "sandboxes/task/worktrees/demo/user-work").exists()


def test_missing_backend_fails_before_mutation(workspace, monkeypatch):
    project, _ = workspace
    text = (project.local / "local.toml").read_text().replace('backend = "venv"', 'backend = "uv"')
    (project.local / "local.toml").write_text(text)
    project = load_project(project.root)
    monkeypatch.setattr(environments.shutil, "which", lambda name: None)
    with pytest.raises(AWMError, match="unavailable"):
        lifecycle.create(project, "task", [])
    assert not (project.local / "state.json").exists()


def test_dependency_conflict_preserves_base(workspace, install_package):
    project, repo = workspace
    install_package(project.base, repo)
    before = environments.inspect(project.base)
    backend = repo / "backend.py"
    backend.write_text(
        backend.read_text().replace(
            "REQUIRES = ''", 'REQUIRES = "Requires-Dist: missing-package==99"'
        )
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "dependency change")
    with pytest.raises(AWMError, match="conflict"):
        lifecycle.create(project, "task", [])
    assert environments.inspect(project.base) == before


def test_symlink_destination_is_rejected(workspace, tmp_path):
    project, _ = workspace
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (project.local / "sandboxes").symlink_to(elsewhere)
    with pytest.raises(AWMError, match="symlink"):
        lifecycle.create(project, "task", [])
    assert list(elsewhere.iterdir()) == []


def test_directory_replacement_and_active_env_are_protected(workspace, tmp_path, monkeypatch):
    project, _ = workspace
    path = make_venv(tmp_path / "owned")
    adopt_env(project, "task", path)
    monkeypatch.setenv("VIRTUAL_ENV", str(path))
    with pytest.raises(AWMError, match="protected"):
        lifecycle.delete(project, ["task"])
    monkeypatch.delenv("VIRTUAL_ENV")
    path.rename(tmp_path / "original-owned")
    make_venv(path)
    with pytest.raises(AWMError, match="identity changed"):
        lifecycle.delete(project, ["task"], force=True)
    assert path.exists()


def test_dirty_batch_preflight_keeps_every_resource(workspace, tmp_path):
    project, repo = workspace
    clean = add_worktree(project, repo, "clean")
    dirty = add_worktree(project, repo, "dirty")
    (dirty / "changed").write_text("keep")
    environment = make_venv(tmp_path / "owned")
    adopt_env(project, "dirty", environment)
    with pytest.raises(AWMError, match="uncommitted"):
        lifecycle.delete(project, ["clean", "dirty"])
    assert clean.exists() and dirty.exists() and environment.exists()


def test_locked_worktree_is_not_force_unlocked(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    git(repo, "worktree", "lock", str(path))
    with pytest.raises(AWMError, match="locked"):
        lifecycle.delete(project, ["t1"], force=True)
    assert path.exists()


def test_concurrent_mutations_and_runs_are_locked(workspace):
    project, _ = workspace
    with lock(project.local / "operation.lock", shared=True):
        with pytest.raises(AWMError, match="busy"):
            lifecycle.create(project, "task", [])
    assert not (project.local / "state.json").exists()


def test_import_idempotence_and_preview(workspace):
    project, repo = workspace
    path = repo.parent / "legacy"
    git(repo, "worktree", "add", "-b", "legacy", str(path))
    migration.import_sandbox(project, "task", [f"demo={path}"], [], dry_run=True)
    assert not (project.local / "state.json").exists()
    first = migration.import_sandbox(project, "task", [f"demo={path}"], [])
    second = migration.import_sandbox(project, "task", [f"demo={path}"], [])
    assert first == second


def test_legacy_import_requires_explicit_naming(workspace, tmp_path):
    project, repo = workspace
    path = repo.parent / "demo_task"
    git(repo, "worktree", "add", "-b", "task", str(path))
    venv_home = tmp_path / "old envs"
    environment = make_venv(venv_home / "custom_task")
    record = migration.import_sandbox(
        project, "task", [], [], legacy=True, venv_home=str(venv_home), base_env="custom"
    )
    assert record["worktrees"][0]["path"] == str(path)
    assert record["environments"][0]["path"] == str(environment)


def test_unselected_environment_in_mixed_import_is_kept(workspace, tmp_path):
    project, _ = workspace
    one, two = [make_venv(tmp_path / n) for n in ("one", "two")]
    migration.import_sandbox(project, "mixed", [], [f"venv={one}", f"venv={two}"])
    lifecycle.delete(project, ["mixed"], keep_worktrees=True, environment_paths={str(one)})
    assert not one.exists() and two.exists()


def test_unsupported_state_fails_closed(workspace):
    project, _ = workspace
    (project.local / "state.json").write_text('{"schema_version": 99}')
    with pytest.raises(AWMError, match="Unsupported"):
        lifecycle.delete(project, ["task"])


def test_kept_nested_environment_prevents_worktree_removal(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    nested = make_venv(path / ".venv")
    adopt_env(project, "t1", nested)
    with pytest.raises(AWMError, match="containing"):
        lifecycle.delete(project, ["t1"], keep_env=True, force=True)
    assert nested.exists() and path.exists()


def test_rollback_does_not_claim_a_later_branch_with_the_same_name(workspace, monkeypatch):
    project, repo = workspace

    def fail(*args):
        raise AWMError("failed build")

    monkeypatch.setattr(environments, "create_environment", fail)
    with pytest.raises(AWMError):
        lifecycle.create(project, "task", [])
    git(repo, "branch", "task")
    lifecycle.delete(project, ["task"], delete_branch=True, force=True)
    assert "task" in git(repo, "branch", "--format=%(refname:short)").splitlines()


def snapshot(packages, python="3.13.0"):
    return {"python": python, "markers": {}, "packages": packages}


def package(name, version, direct_url=None):
    return {
        "name": name,
        "version": version,
        "direct_url": direct_url,
        "requires": [],
        "installer": "pip",
        "metadata_path": None,
    }


def test_shared_editable_version_follows_its_source(workspace, install_package, tmp_path):
    project, repo = workspace
    install_package(project.base, repo)
    shared = install_package(project.base, tmp_path / "shared source", "shared_pkg")
    backend = shared / "backend.py"
    backend.write_text(backend.read_text().replace('VERSION = "1.0"', 'VERSION = "2.0"'))
    # Same-size edits keep stale bytecode valid when both writes share an mtime second.
    shutil.rmtree(shared / "__pycache__", ignore_errors=True)
    record = lifecycle.create(project, "task", [])
    actual = environments.inspect(Path(record["environments"][0]["path"]))
    assert {p["name"]: p["version"] for p in actual["packages"]}["shared_pkg"] == "2.0"
    assert {e["name"]: e["shared"] for e in record["editables"]}["shared_pkg"] is True
    lifecycle.delete(project, ["task"])


def test_pinned_version_drift_is_still_rejected():
    record = {
        "base_snapshot": snapshot([package("pinned_pkg", "1.0")]),
        "install_editables": {},
        "worktrees": [],
    }
    with pytest.raises(AWMError, match=r"drift: pinned-pkg 1\.0 -> 2\.0"):
        lifecycle.verify_environment(record, snapshot([package("pinned_pkg", "2.0")]))


def test_unexpected_package_is_rejected():
    record = {"base_snapshot": snapshot([]), "install_editables": {}, "worktrees": []}
    with pytest.raises(AWMError, match="Unexpected package in sandbox: extra-pkg"):
        lifecycle.verify_environment(record, snapshot([package("extra_pkg", "1.0")]))
