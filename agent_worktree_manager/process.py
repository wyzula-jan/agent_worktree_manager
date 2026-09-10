"""Subprocess boundaries shared by backends and Git operations."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .errors import AWMError


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "AWM_SHELL",
        "AWM_SHELL_HANDOFF",
    ):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float = 120,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    try:
        with subprocess.Popen(
            args,
            cwd=cwd,
            env=env if env is not None else clean_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                # Package builders may have descendants; stop the entire attempt before rollback.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AWMError(f"Cannot run {args[0]}: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AWMError(f"{Path(args[0]).name} failed: {detail}")
    return result
