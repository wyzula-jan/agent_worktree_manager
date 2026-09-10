import os
import sys
import time

from conftest import make_venv

from agent_worktree_manager import inspection, migration, tui


def test_python_version_from_pyvenv_cfg(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("version = 3.12.3\n")
    assert inspection.python_version_of(tmp_path) == "3.12.3"


def test_python_version_from_conda_meta(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    (tmp_path / "conda-meta/python-3.12.11-build.json").write_text("{}")
    assert inspection.python_version_of(tmp_path) == "3.12.11"


def test_python_version_unknown_or_absent(tmp_path):
    assert inspection.python_version_of(tmp_path) == "-"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/python").touch()
    assert inspection.python_version_of(tmp_path) == "?"


def test_env_changed_ts_uses_newest_stamp(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("version = 3.12\n")
    old = time.time() - 86400
    os.utime(tmp_path, (old, old))
    assert inspection.env_changed_ts(tmp_path) > old


def test_gather_envs_marks_sandbox_and_protected(workspace, tmp_path):
    project, _ = workspace
    owned = make_venv(tmp_path / "owned")
    unrelated = make_venv(tmp_path / "unrelated")
    migration.import_sandbox(project, "task", [], [f"venv={owned}"])
    by_path = {i.env.path: i for i in inspection.gather_envs(project)}
    assert by_path[str(project.base)].protected
    assert by_path[str(unrelated)].protected
    assert by_path[str(owned)].sandbox == "task" and not by_path[str(owned)].protected


def test_gather_envs_protects_active_env(workspace, tmp_path, monkeypatch):
    project, _ = workspace
    owned = make_venv(tmp_path / "owned")
    migration.import_sandbox(project, "task", [], [f"venv={owned}"])
    monkeypatch.setenv("VIRTUAL_ENV", str(owned))
    assert next(i for i in inspection.gather_envs(project) if i.env.path == str(owned)).protected


def test_gather_envs_without_conda(workspace, monkeypatch):
    project, _ = workspace

    def fail():
        raise AssertionError("unselected conda backend must not be queried")

    monkeypatch.setattr(inspection.environments, "conda_environments", fail)
    assert inspection.gather_envs(project)


def test_list_packages_with_real_interpreter():
    packages, error = inspection.list_packages(sys.executable)
    assert error is None
    assert "pytest" in {p.name.lower() for p in packages}
    assert packages == sorted(packages, key=lambda p: p.name.lower())


def test_list_packages_reports_editable_installs(workspace, install_package):
    project, repo = workspace
    install_package(project.base, repo)
    packages, error = inspection.list_packages(str(project.base / "bin/python"))
    assert error is None and packages[0].editable == str(repo)


def test_list_packages_missing_interpreter(tmp_path):
    packages, error = inspection.list_packages(str(tmp_path / "nope"))
    assert not packages and "no python" in error


def test_list_packages_broken_interpreter(tmp_path):
    path = tmp_path / "python"
    path.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    path.chmod(0o755)
    packages, error = inspection.list_packages(str(path))
    assert not packages and "boom" in error


def test_matches_is_grep_like():
    assert inspection._matches("my package 1.0", "MY 1.0")
    assert not inspection._matches("my package 1.0", "missing")


def test_pkg_overlay_filters():
    env = inspection.EnvItem(
        inspection.Env("env", "/env", "venv"),
        pkgs=[inspection.Pkg("alpha", "1.0", None), inspection.Pkg("beta", "2.0", None)],
    )
    overlay = tui.PkgOverlay(env, filter="beta 2")
    assert [p.name for p in overlay.visible] == ["beta"]


def test_size_worker_serves_priority_first(tmp_path, monkeypatch):
    worker = inspection.SizeWorker()
    worker._queue = ["a", "b", "c"]
    worker.prioritize("c")
    assert [worker._next(), worker._next(), worker._next(), worker._next()] == ["c", "a", "b", None]
