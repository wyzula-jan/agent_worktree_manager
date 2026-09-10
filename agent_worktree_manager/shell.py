"""Hand activation back to the invoking shell after the TUI exits."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

from .environments import executable
from .errors import AWMError
from .storage import atomic_text


def shell_init(shell: str) -> str:
    if shell not in ("bash", "zsh"):
        raise AWMError("Shell integration supports bash and zsh")
    # Pin the tool interpreter: activating a project must not switch AWM itself.
    python = shlex.quote(sys.executable)
    return f"""awm() {{
    local _awm_dir _awm_code _awm_result
    _awm_dir="$(command mktemp -d "${{TMPDIR:-/tmp}}/awm-shell.XXXXXXXX")" || return
    if AWM_SHELL={shell} AWM_SHELL_HANDOFF="$_awm_dir/activate" command {python} -I -m agent_worktree_manager "$@"; then
        _awm_result=0
        _awm_code=""
        if [ -f "$_awm_dir/activate" ]; then
            _awm_code="$(command cat "$_awm_dir/activate")" || _awm_result=$?
        fi
    else
        _awm_result=$?
    fi
    command rm -f "$_awm_dir/activate"
    command rmdir "$_awm_dir"
    if [ "$_awm_result" -ne 0 ]; then
        return "$_awm_result"
    fi
    if [ -n "$_awm_code" ]; then
        eval "$_awm_code"
    fi
}}
"""


def handoff_target() -> tuple[Path, str]:
    destination = os.environ.get("AWM_SHELL_HANDOFF")
    shell = os.environ.get("AWM_SHELL")
    if not destination or shell not in ("bash", "zsh"):
        raise AWMError('Enable first: eval "$(awm shell-init zsh)" (or bash)')
    return Path(destination), shell


def write_handoff(destination: Path, script: str) -> None:
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise AWMError("Shell handoff requires a private temporary directory; reload shell-init")
    atomic_text(destination, script)


def activation_script(kind: str, path: Path, cwd: Path, shell: str) -> str:
    if shell not in ("bash", "zsh"):
        raise AWMError("Shell integration supports bash and zsh")
    if not (path / "bin/python").is_file():
        raise AWMError(f"Missing environment interpreter: {path}")
    if not cwd.is_dir():
        raise AWMError(f"Missing working directory: {cwd}")
    if kind == "conda":
        # Conda's native shell output does not escape these prefix characters.
        if any(character in str(path) for character in "'\n\r"):
            raise AWMError("Conda activation requires an environment path without quotes/newlines")
        conda = shlex.quote(executable("conda"))
        # Initialize the native shell function even when conda init was not run.
        activate = (
            "local _awm_conda_hook\n"
            f'_awm_conda_hook="$(command {conda} shell.{shell} hook)" || return\n'
            'eval "$_awm_conda_hook" || return\n'
            "if typeset -f deactivate >/dev/null 2>&1; then deactivate || return; fi\n"
            f"conda activate {shlex.quote(str(path))} || return\n"
        )
    else:
        script = path / "bin/activate"
        if not script.is_file():
            raise AWMError(f"Missing environment activation script: {script}")
        activate = f". {shlex.quote(str(script))} || return\n"
    return activate + f"cd -- {shlex.quote(str(cwd))}\n"
