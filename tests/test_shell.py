"""Sandbox shells keep the selected interpreter and the lifecycle lock."""

import errno
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_venv

from agent_worktree_manager import environments, lifecycle, migration
from agent_worktree_manager.errors import AWMError


def test_shell_holds_lock_and_supports_environment_only_import(workspace, tmp_path, monkeypatch):
    project, _ = workspace
    env = make_venv(tmp_path / "owned")
    migration.import_sandbox(project, "task", [], [f"venv={env}"])
    monkeypatch.setenv("SHELL", "/bin/bash")

    def run(kind, target, args, cwd, *, interactive):
        assert (kind, target, cwd, interactive) == ("venv", env, project.root, True)
        assert args[1:] == ["--noprofile", "--norc", "-i"]
        with pytest.raises(AWMError, match="busy"):
            lifecycle.delete(project, ["task"])
        return 0

    monkeypatch.setattr(environments, "run_environment", run)
    assert lifecycle.open_shell(project, "task") == 0
    lifecycle.delete(project, ["task"])


def test_shell_requires_explicit_environment_and_rejects_missing_resources(workspace, tmp_path):
    project, _ = workspace
    first, second = make_venv(tmp_path / "first"), make_venv(tmp_path / "second")
    migration.import_sandbox(project, "task", [], [f"venv={first}", f"uv={second}"])
    with pytest.raises(AWMError, match="Select one"):
        lifecycle.open_shell(project, "task")
    shutil.rmtree(second)
    with pytest.raises(AWMError, match="missing"):
        lifecycle.open_shell(project, "task", environment=str(second))


def test_shell_rejects_failed_sandbox(workspace):
    project, _ = workspace
    state = lifecycle.read_state(project)
    record = lifecycle.sandbox_record("failed")
    record["status"] = "failed"
    state["sandboxes"]["failed"] = record
    lifecycle.save_state(project, state)
    with pytest.raises(AWMError, match="not ready"):
        lifecycle.open_shell(project, "failed")


def test_conda_shell_uses_native_activation_and_waits_through_interrupt(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setattr(environments, "executable", lambda kind: "/conda")
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated")
    monkeypatch.setenv("PYTHONPATH", "/unrelated")
    monkeypatch.setenv("ENV", "/unrelated/init")

    class Process:
        def __init__(self, args, **kwargs):
            observed.update(args=args, **kwargs)
            self.waits = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    monkeypatch.setattr(subprocess, "Popen", Process)
    assert (
        environments.run_environment(
            "conda", tmp_path, ["/bin/bash", "-i"], tmp_path, interactive=True
        )
        == 0
    )
    assert observed["args"] == [
        "/conda",
        "run",
        "--no-capture-output",
        "--prefix",
        str(tmp_path),
        "/bin/bash",
        "-i",
    ]
    assert not {"VIRTUAL_ENV", "PYTHONPATH", "ENV"} & observed["env"].keys()


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.integration
def test_tui_shell_activates_environment_and_returns_to_browser(
    workspace, tmp_path, monkeypatch, shell
):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} not installed")
    project, _ = workspace
    record = lifecycle.create(project, "task", [])
    env = Path(record["environments"][0]["path"])
    worktree = Path(record["worktrees"][0]["path"])
    startup = tmp_path / "shell startup"
    startup.mkdir()
    (startup / ".zshrc").write_text("export PATH=/wrong-environment\n")
    monkeypatch.setenv("SHELL", executable)
    monkeypatch.setenv("ZDOTDIR", str(startup))
    monkeypatch.setenv("ENV", str(startup / ".zshrc"))
    monkeypatch.setenv("TERM", "xterm")
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(
            sys.executable,
            [sys.executable, "-m", "agent_worktree_manager", "--project", project.id],
        )
    reaped = False

    def read_until(marker):
        output = b""
        deadline = time.monotonic() + 15
        while marker not in output and time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output += chunk
        assert marker in output, output.decode(errors="replace")

    try:
        read_until(b"[s] shell")
        os.write(terminal, b"s")
        read_until(b"(awm) $ ")
        os.write(terminal, b"\x03")
        read_until(b"(awm) $ ")
        with pytest.raises(AWMError, match="busy"):
            lifecycle.delete(project, ["task"])
        command = (
            "python -c 'import json,sys,os; from pathlib import Path; "
            'Path("shell-result.json").write_text(json.dumps([sys.prefix,os.getcwd(),os.environ.get("VIRTUAL_ENV")]))\'\n'
        )
        os.write(terminal, command.encode())
        read_until(b"(awm) $ ")
        assert json.loads((worktree / "shell-result.json").read_text()) == [
            str(env),
            str(worktree),
            str(env),
        ]
        os.write(terminal, b"exit\n")
        read_until(b"[s] shell")
        os.write(terminal, b"q")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.05)[0]:
                try:
                    os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 0
                break
            time.sleep(0.05)
        assert reaped, "TUI did not exit after returning from shell"
    finally:
        os.close(terminal)
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    (worktree / "shell-result.json").unlink()
    lifecycle.delete(project, ["task"])
