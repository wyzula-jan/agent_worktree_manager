"""All tests own their Git repositories, environments and registry."""

from __future__ import annotations

import subprocess
import sys

import pytest

from agent_worktree_manager import environments, registry
from agent_worktree_manager.config import initialize


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "f.txt").write_text("original\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def make_venv(path):
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(path)], check=True, capture_output=True
    )
    return path


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_DATA_HOME", str(tmp_path / "registry"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))


@pytest.fixture
def make_workspace(tmp_path):
    def make(name="project", backend="venv", base=None):
        root = tmp_path / name
        root.mkdir()
        repo = make_repo(root / "demo")
        base_path = base or make_venv(tmp_path / f"{name}-base")
        project = initialize(root, base_path, backend, name, ["demo=demo"])
        registry.register(project)
        return project, repo

    return make


@pytest.fixture
def workspace(make_workspace):
    return make_workspace()


BACKEND = r"""
from pathlib import Path
import zipfile
NAME = "demo_pkg"
VERSION = "1.0"
REQUIRES = ""
def _build(wheel_directory, editable):
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    dist = f"{NAME}-{VERSION}.dist-info"
    with zipfile.ZipFile(Path(wheel_directory) / filename, "w") as z:
        if editable:
            z.writestr(f"{NAME}.pth", str(Path.cwd()) + "\n")
        else:
            z.write(Path(NAME) / "__init__.py", f"{NAME}/__init__.py")
        z.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n{REQUIRES}\n")
        z.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nGenerator: awm-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{dist}/entry_points.txt", f"[console_scripts]\n{NAME}-where = {NAME}:main\n")
        z.writestr(f"{dist}/RECORD", "")
    return filename
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, False)
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, True)
"""


@pytest.fixture
def install_package(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setenv("UV_FIND_LINKS", str(wheelhouse))

    def install(base, directory, name="demo_pkg", editable=True, requires=""):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).mkdir(exist_ok=True)
        (directory / name / "__init__.py").write_text(
            'VALUE = "original"\ndef main():\n    print(__file__)\n'
        )
        (directory / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        )
        (directory / "backend.py").write_text(
            BACKEND.replace('NAME = "demo_pkg"', f'NAME = "{name}"').replace(
                'REQUIRES = ""', f"REQUIRES = {requires!r}"
            )
        )
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                "import backend; print(backend.build_wheel(" + repr(str(wheelhouse)) + "))",
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
        target = str(directory) if editable else str(wheelhouse / output.stdout.strip())
        environments.install(
            "venv", base, [] if editable else [target], [target] if editable else []
        )
        # Make non-editable installs representative of an index-installed dependency.
        if not editable:
            script = (
                "import importlib.metadata as m; from pathlib import Path; d=m.distribution("
                + repr(name)
                + "); Path(d._path, 'direct_url.json').unlink(missing_ok=True)"
            )
            subprocess.run([str(base / "bin/python"), "-c", script], check=True)
        if (directory / ".git").exists():
            git(directory, "add", ".")
            git(directory, "commit", "-m", "package")
        return directory

    return install
