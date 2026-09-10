import json

import pytest
from conftest import make_venv
from test_cli import add_worktree

from agent_worktree_manager import cli, migration, registry
from agent_worktree_manager.config import discover_root, editable_target, initialize, load_project
from agent_worktree_manager.errors import AWMError


def test_global_install_manages_two_independent_projects(
    make_workspace, tmp_path, monkeypatch, capsys
):
    first, first_repo = make_workspace("first")
    second, second_repo = make_workspace("second")
    first_wt = add_worktree(first, first_repo, "same")
    second_wt = add_worktree(second, second_repo, "same")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["list", "--all", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {p["id"] for p in result["projects"]} == {first.id, second.id}
    assert cli.main(["delete", "--project", "first", "same", "--yes"]) == 0
    assert not first_wt.exists() and second_wt.exists()
    monkeypatch.chdir(second_repo)
    assert discover_root() == second.root
    assert registry.select().id == second.id


def test_shared_base_and_other_project_resources_are_protected(make_workspace, tmp_path):
    first, repo = make_workspace("first")
    second, _ = make_workspace("second", base=first.base)
    with pytest.raises(AWMError, match="protected"):
        migration.import_sandbox(second, "bad", [], [f"venv={first.base}"])
    external = make_venv(tmp_path / "external")
    migration.import_sandbox(first, "task", [], [f"venv={external}"])
    with pytest.raises(AWMError, match="already belongs"):
        migration.import_sandbox(second, "task", [], [f"venv={external}"])


def test_unregister_preserves_project_data(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    before = (project.local / "state.json").read_bytes()
    registry.unregister(project.id)
    assert not registry.entries()
    assert path.exists() and project.base.exists()
    assert (project.local / "state.json").read_bytes() == before
    registry.register(project)
    assert len(registry.entries()) == 1


def test_unavailable_project_is_listed(workspace, capsys):
    project, _ = workspace
    project.root.rename(project.root.with_name("moved"))
    assert cli.main(["projects", "list", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "unavailable" in result["projects"][0]
    registry.unregister(project.id)


def test_selectors_and_duplicate_display_names(make_workspace, monkeypatch, tmp_path):
    first, _ = make_workspace("first")
    second, _ = make_workspace("second")
    path = second.root / "awm.toml"
    path.write_text(path.read_text().replace('name = "second"', 'name = "first"'))
    registry.register(registry.select(root=str(second.root)))
    with pytest.raises(AWMError, match="ambiguous"):
        registry.select(project="first")
    assert registry.select(project=first.id).id == first.id
    with pytest.raises(AWMError, match="either"):
        registry.select(project=first.id, root=str(first.root))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["delete", "task", "--yes"]) == 1
    assert cli.main(["--root", str(first.root), "list", "--project", first.id]) == 1


def test_config_is_discovered_from_subdirectory(workspace, monkeypatch):
    project, repo = workspace
    nested = repo / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cli.main(["list"]) == 0
    assert cli.main(["--root", str(project.root), "list"]) == 0


def test_registry_corruption_is_not_overwritten(workspace):
    project, _ = workspace
    registry.registry_path().write_text("invalid")
    with pytest.raises(AWMError):
        registry.register(project)
    assert registry.registry_path().read_text() == "invalid"


def test_invalid_shared_config_does_not_write_local_state(tmp_path):
    from conftest import make_repo

    root = tmp_path / "bad config"
    root.mkdir()
    make_repo(root / "repo")
    (root / "awm.toml").write_text('schema_version = 1\nname = "broken"\n')
    base = make_venv(tmp_path / "base")
    with pytest.raises(AWMError, match="Invalid project"):
        initialize(root, base, "venv", None, [])
    assert not (root / ".awm").exists()


def test_init_default_repo_supports_spaces_in_project_path(tmp_path):
    from conftest import make_repo

    root = make_repo(tmp_path / "project with spaces")
    project = initialize(root, make_venv(tmp_path / "base"), "venv", None, [])
    assert project.repos["repo"].path == root


def test_local_repository_overrides_preserve_shared_config(workspace, tmp_path):
    from conftest import make_repo

    project, _ = workspace
    checkout = make_repo(tmp_path / "local checkout")
    shared = (project.root / "awm.toml").read_bytes()
    local = project.local / "local.toml"
    with local.open("a") as stream:
        stream.write(
            f'\n[repositories.demo]\npath = {json.dumps(str(checkout))}\ntargets = [".[dev, docs]"]\n'
        )
    loaded = load_project(project.root)
    assert loaded.repos["demo"].path == checkout
    assert editable_target(loaded.repos["demo"].targets[0]) == (".", ("dev", "docs"))
    assert (project.root / "awm.toml").read_bytes() == shared


def test_local_overrides_reject_unknown_alias(workspace):
    project, _ = workspace
    with (project.local / "local.toml").open("a") as stream:
        stream.write('\n[repositories.typo]\npath = "../other"\n')
    with pytest.raises(AWMError, match="configured aliases"):
        load_project(project.root)
