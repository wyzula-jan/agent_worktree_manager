"""Activation happens in the invoking shell after the TUI has exited."""

import errno
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from conftest import make_venv

from agent_worktree_manager import lifecycle, migration, shell
from agent_worktree_manager.errors import AWMError
from agent_worktree_manager.process import run


def test_activation_validates_resources_and_supports_environment_only_import(workspace, tmp_path):
    project, _ = workspace
    env = make_venv(tmp_path / "owned")
    migration.import_sandbox(project, "task", [], [f"venv={env}"])
    script = lifecycle.prepare_activation(project, "task", "zsh")
    assert str(env / "bin/activate") in script and str(project.root) in script
    (env / "bin/activate").unlink()
    with pytest.raises(AWMError, match="Missing environment activation"):
        lifecycle.prepare_activation(project, "task", "zsh")
    lifecycle.delete(project, ["task"])


def test_activation_requires_explicit_environment_and_rejects_missing_resources(
    workspace, tmp_path
):
    project, _ = workspace
    first, second = make_venv(tmp_path / "first"), make_venv(tmp_path / "second")
    migration.import_sandbox(project, "task", [], [f"venv={first}", f"uv={second}"])
    with pytest.raises(AWMError, match="Select one"):
        lifecycle.prepare_activation(project, "task", "bash")
    shutil.rmtree(second)
    with pytest.raises(AWMError, match="missing"):
        lifecycle.prepare_activation(project, "task", "bash", environment=str(second))


def test_activation_rejects_failed_sandbox(workspace):
    project, _ = workspace
    state = lifecycle.read_state(project)
    record = lifecycle.sandbox_record("failed")
    record["status"] = "failed"
    state["sandboxes"]["failed"] = record
    lifecycle.save_state(project, state)
    with pytest.raises(AWMError, match="not ready"):
        lifecycle.prepare_activation(project, "failed", "zsh")


def test_handoff_requires_shell_integration_and_private_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("AWM_SHELL_HANDOFF", raising=False)
    with pytest.raises(AWMError, match="shell-init"):
        shell.handoff_target()
    destination = tmp_path / "handoff"
    destination.mkdir(mode=0o755)
    with pytest.raises(AWMError, match="private"):
        shell.write_handoff(destination / "activate", "# script")
    destination.chmod(0o700)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    (destination / "activate").symlink_to(unrelated)
    with pytest.raises(AWMError, match="symlink"):
        shell.write_handoff(destination / "activate", "# script")
    assert unrelated.read_text() == "unchanged"


@pytest.mark.parametrize("shell_name", ["bash", "zsh"])
def test_wrapper_preserves_exit_status_and_cleans_handoff(tmp_path, shell_name):
    executable = shutil.which(shell_name)
    if not executable:
        pytest.skip(f"{shell_name} not installed")
    result = subprocess.run(
        [executable, "-c", shell.shell_init(shell_name) + "\nawm --invalid; exit $?"],
        env={**os.environ, "TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, result.stderr
    assert not list(tmp_path.glob("awm-shell.*"))


@pytest.mark.parametrize("shell_name", ["bash", "zsh"])
@pytest.mark.parametrize("kind", ["venv", "uv", "conda"])
@pytest.mark.integration
def test_tui_activates_in_original_shell(make_workspace, tmp_path, monkeypatch, shell_name, kind):
    executable = shutil.which(shell_name)
    if not executable:
        pytest.skip(f"{shell_name} not installed")
    if kind == "conda" and not (os.environ.get("CONDA_EXE") or shutil.which("conda")):
        pytest.skip("conda not installed")
    project, _ = make_workspace(
        "project with spaces" if kind == "conda" else "project ' $(touch INJECTED) ;"
    )
    record = lifecycle.create(project, "task", [])
    env = Path(record["environments"][0]["path"])
    worktree = Path(record["worktrees"][0]["path"])
    if kind != "venv":
        if kind == "conda":
            # Activation only needs an existing prefix, without package downloads.
            (env / "conda-meta").mkdir()
            (env / "conda-meta/history").touch()
        state = lifecycle.read_state(project)
        state["sandboxes"]["task"]["environments"][0]["kind"] = kind
        lifecycle.save_state(project, state)
    result_file = tmp_path / "result.json"
    capture = (
        "import json,sys,os; from pathlib import Path; "
        f"Path({str(result_file)!r}).write_text(json.dumps([sys.prefix,os.getcwd(),"
        "os.getppid(),os.environ.get('VIRTUAL_ENV'),os.environ.get('CONDA_PREFIX')]))"
    )
    startup = (
        shell.shell_init(shell_name)
        + "\n_awm_original_level=$SHLVL\n"
        + 'alias awm_test_alias="printf alias-preserved"\n'
        + f"awm --project {project.id} || exit 91\n"
        + '[ "$SHLVL" = "$_awm_original_level" ] || exit 92\n'
        + "alias awm_test_alias >/dev/null || exit 93\n"
        + f"python -c {shlex.quote(capture)} || exit 94\n"
        + "awm --version || exit 95\n"
        + f"if awm --project {project.id} delete task --yes; then exit 96; fi\n"
        + ("conda deactivate\n" if kind == "conda" else "deactivate\n")
        + "exit 0\n"
    )
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SHELL", "/not-the-running-shell")
    options = ["--noprofile", "--norc"] if shell_name == "bash" else ["-f"]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(executable, [executable, *options, "-i", "-c", startup])
    reaped = False
    output = b""
    try:
        deadline = time.monotonic() + 30
        selected = False
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.05)[0]:
                try:
                    output += os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not selected and b"[s] activate" in output:
                os.write(terminal, b"s")
                selected = True
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors="replace")
                break
        assert reaped, output.decode(errors="replace")
        assert selected
    finally:
        os.close(terminal)
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    result = json.loads(result_file.read_text())
    assert result[:3] == [str(env), str(worktree), pid]
    assert result[4 if kind == "conda" else 3] == str(env)
    assert not (worktree / "INJECTED").exists()
    assert not list(tmp_path.glob("awm-shell.*"))
    assert env.is_dir()
    lifecycle.delete(project, ["task"])


def test_handoff_is_not_inherited_by_project_commands(monkeypatch):
    import sys

    monkeypatch.setenv("AWM_SHELL_HANDOFF", "/parent/handoff")
    monkeypatch.setenv("AWM_SHELL", "zsh")
    result = run([sys.executable, "-c", "import os; assert 'AWM_SHELL_HANDOFF' not in os.environ"])
    assert result.returncode == 0


def test_conda_rejects_paths_unsafe_for_native_activation(tmp_path):
    env = make_venv(tmp_path / "quote'prefix")
    with pytest.raises(AWMError, match="without quotes"):
        shell.activation_script("conda", env, tmp_path, "zsh")
