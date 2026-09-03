"""Environment browser data layer: discovery, metadata, protection, package listing."""

import os
import sys
import time
from pathlib import Path

import pytest

from agent_worktree_manager import cli, tui


def _make_venv(venv_home: Path, name: str, version: str = "3.13.7", real_python=False) -> Path:
    venv = venv_home / name
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    if real_python:
        os.symlink(sys.executable, py)
    else:
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
    (venv / "pyvenv.cfg").write_text(f"home = /usr\nversion_info = {version}\n")
    return venv


def _make_conda_env(base: Path, name: str, version: str = "3.12.11") -> Path:
    env = base / "envs" / name
    (env / "conda-meta").mkdir(parents=True)
    (env / "conda-meta" / f"python-{version}-hc22306f_0_cpython.json").write_text("{}")
    (env / "bin").mkdir()
    (env / "bin" / "python").write_text("")
    return env


def test_python_version_from_pyvenv_cfg(tmp_path):
    venv = _make_venv(tmp_path, "v", version="3.11.9")
    assert tui.python_version_of(venv) == "3.11.9"


def test_python_version_from_conda_meta(tmp_path):
    env = _make_conda_env(tmp_path, "c", version="3.10.4")
    assert tui.python_version_of(env) == "3.10.4"


def test_python_version_unknown_or_absent(tmp_path):
    (tmp_path / "empty").mkdir()
    assert tui.python_version_of(tmp_path / "empty") == "-"
    (tmp_path / "bare" / "bin").mkdir(parents=True)
    (tmp_path / "bare" / "bin" / "python").write_text("")
    assert tui.python_version_of(tmp_path / "bare") == "?"


def test_env_changed_ts_uses_newest_stamp(tmp_path):
    venv = _make_venv(tmp_path, "v")
    sp = venv / "lib" / "python3.13" / "site-packages"
    sp.mkdir(parents=True)
    old = time.time() - 86400 * 30
    os.utime(venv, (old, old))
    os.utime(venv / "pyvenv.cfg", (old, old))
    ts = tui.env_changed_ts(venv)
    assert ts is not None and ts > old + 86400 * 29  # site-packages is fresh


def test_gather_envs_marks_sandbox_and_protected(tmp_path, monkeypatch):
    venv_home = tmp_path / ".venvs"
    _make_venv(venv_home, "bec_base")
    _make_venv(venv_home, "bec_base_t1")
    _make_venv(venv_home, "unrelated")
    conda_root = tmp_path / "miniforge3"
    _make_conda_env(conda_root, "bec_312")
    _make_conda_env(conda_root, "bec_312_x")
    _make_conda_env(conda_root, "other")
    (conda_root / "conda-meta").mkdir()
    conda_map = {
        "miniforge3": str(conda_root),
        "bec_312": str(conda_root / "envs" / "bec_312"),
        "bec_312_x": str(conda_root / "envs" / "bec_312_x"),
        "other": str(conda_root / "envs" / "other"),
    }
    monkeypatch.setattr(cli, "conda_envs", lambda conda: conda_map)
    monkeypatch.setattr(tui, "_active_prefixes", lambda: set())

    items = tui.gather_envs(venv_home, "/fake/conda", "bec_base", "bec_312")
    by_name = {i.name: i for i in items}
    assert [i.name for i in items[:3]] == ["bec_base", "bec_base_t1", "unrelated"]  # uv first
    assert by_name["bec_base"].protected == "base env"
    assert by_name["bec_base_t1"].sandbox == "t1" and by_name["bec_base_t1"].protected is None
    assert by_name["unrelated"].sandbox is None
    assert by_name["bec_312"].protected == "base env"
    assert by_name["bec_312_x"].sandbox == "x"
    assert by_name["base"].protected == "conda installation root"
    assert by_name["base"].env.path == str(conda_root)
    assert by_name["other"].python == "3.12.11"


def test_gather_envs_protects_active_env(tmp_path, monkeypatch):
    venv_home = tmp_path / ".venvs"
    venv = _make_venv(venv_home, "work")
    monkeypatch.setattr(tui, "_active_prefixes", lambda: {str(venv.resolve())})
    items = tui.gather_envs(venv_home, None, "bec_base", "bec_312")
    assert items[0].protected == "currently active env"


def test_gather_envs_without_conda(tmp_path):
    venv_home = tmp_path / ".venvs"
    _make_venv(venv_home, "only")
    items = tui.gather_envs(venv_home, None, "bec_base", "bec_312")
    assert [i.name for i in items] == ["only"]


def test_list_packages_with_real_interpreter():
    pkgs, error = tui.list_packages(sys.executable)
    assert error is None
    names = {p.name.lower() for p in pkgs}
    assert "pytest" in names
    assert pkgs == sorted(pkgs, key=lambda p: p.name.lower())
    assert len(names) == len(pkgs)  # de-duplicated


def test_list_packages_reports_editable_installs():
    pkgs, _ = tui.list_packages(sys.executable)
    editable = [p for p in pkgs if p.editable]
    # the test env has this very package installed editable (or nothing editable at all);
    # either way every reported path must exist
    for p in editable:
        assert Path(p.editable).exists()


def test_list_packages_missing_interpreter(tmp_path):
    pkgs, error = tui.list_packages(str(tmp_path / "nope"))
    assert pkgs == [] and "no python interpreter" in error


def test_list_packages_broken_interpreter(tmp_path):
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\necho 'boom: bad interpreter' >&2\nexit 3\n")
    py.chmod(0o755)
    pkgs, error = tui.list_packages(str(py))
    assert pkgs == [] and "boom" in error


def test_matches_is_grep_like():
    assert tui._matches("bec_widgets 3.1 /Users/x/bec_widgets_data_api", "widgets")
    assert tui._matches("bec_widgets 3.1", "WIDGETS 3.1")
    assert not tui._matches("bec_widgets 3.1", "widgets 4")
    assert tui._matches("anything", "")


def test_pkg_overlay_filters():
    env = tui.EnvItem(env=cli.Env("e", "/x", "uv"))
    env.pkgs = [tui.Pkg("numpy", "2.0", None), tui.Pkg("bec_lib", "3.0", "/wt/bec/bec_lib")]
    ov = tui.PkgOverlay(env=env)
    assert [p.name for p in ov.visible] == ["numpy", "bec_lib"]
    ov.filter = "/wt/"
    assert [p.name for p in ov.visible] == ["bec_lib"]


def test_size_worker_serves_priority_first(tmp_path, monkeypatch):
    calls = []

    def fake_du(path):
        calls.append(str(path))
        return 1

    monkeypatch.setattr(tui, "_du_kb", fake_du)
    tui.SIZES.clear()
    worker = tui.SizeWorker()
    paths = [str(tmp_path / f"p{i}") for i in range(5)]
    worker.prioritize(paths[3])
    worker.add(paths)
    deadline = time.time() + 5
    while len(tui.SIZES) < 5 and time.time() < deadline:
        time.sleep(0.01)
    assert calls[0] == paths[3]
    assert all(tui.SIZES[p] == 1 for p in paths)
