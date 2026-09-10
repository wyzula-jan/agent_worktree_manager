import sys
import time

import pytest

from agent_worktree_manager import environments
from agent_worktree_manager.errors import AWMError
from agent_worktree_manager.process import run


def test_timeout_stops_builder_descendants(tmp_path):
    marker = tmp_path / "late-write"
    child = (
        "import time; from pathlib import Path; time.sleep(0.8); Path("
        + repr(str(marker))
        + ").write_text('late')"
    )
    parent = (
        "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
        + repr(child)
        + "]); time.sleep(10)"
    )
    with pytest.raises(AWMError, match="timed out"):
        run([sys.executable, "-c", parent], timeout=0.2)
    time.sleep(1)
    assert not marker.exists()


def test_installer_cannot_be_redirected_into_base(workspace, install_package, monkeypatch):
    project, repo = workspace
    monkeypatch.setenv("PIP_TARGET", str(project.base / "redirected"))
    monkeypatch.setenv("PIP_PREFIX", str(project.base))
    install_package(project.base, repo)
    assert not (project.base / "redirected").exists()
    assert environments.inspect(project.base)["packages"][0]["name"] == "demo_pkg"
