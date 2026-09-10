"""Inspect and reproduce installed environments without writing to the base."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .errors import AWMError
from .process import clean_env, run


@dataclass
class Env:
    name: str
    path: str
    kind: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind})"


INSPECT = r"""
import importlib.metadata as md
import json, os, platform, re, sys, sysconfig
from pathlib import Path
packages = []
metadata_paths = {}
# Editable .pth files can expose stale source egg-info outside the environment.
# Only installed metadata belongs in the inventory; source URLs come from it.
environment_paths = [root for root in sys.path
    if Path(root).resolve().is_relative_to(Path(sys.prefix).resolve())]
for root in environment_paths:
    root = Path(root)
    if root.is_dir():
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.endswith(('.dist-info', '.egg-info')):
                distribution = md.PathDistribution(entry)
                key = (re.sub(r'[-_.]+', '-', distribution.metadata.get('Name', '')).lower(), distribution.version)
                for filename in ('METADATA', 'PKG-INFO'):
                    if (entry / filename).is_file():
                        metadata_paths[key] = entry / filename
                        break
for d in md.distributions(path=environment_paths):
    name = d.metadata.get('Name')
    if not name:
        raise RuntimeError('Installed distribution has no Name metadata')
    direct = d.read_text('direct_url.json')
    expected = re.sub(r'[-_.]+', '-', name + '-' + d.version + '.dist-info').lower()
    metadata_file = next((d.locate_file(f) for f in d.files or [] if f.name == 'METADATA'
        and re.sub(r'[-_.]+', '-', f.parent.name).lower() == expected), None)
    if metadata_file is None:
        metadata_file = metadata_paths.get((re.sub(r'[-_.]+', '-', name).lower(), d.version))
    packages.append(dict(name=name, version=d.version,
        direct_url=json.loads(direct) if direct else None,
        requires=d.requires or [], installer=(d.read_text('INSTALLER') or '').strip(),
        metadata_path=str(metadata_file) if metadata_file else None))
site = Path(sysconfig.get_path('purelib'))
if list(site.glob('*.egg-link')):
    raise RuntimeError('Legacy egg-link editables are unsupported; reinstall them using PEP 660 first')
impl = sys.implementation.version
iv = '.'.join(str(x) for x in impl[:3])
if impl.releaselevel != 'final':
    iv += impl.releaselevel[0] + str(impl.serial)
markers = dict(implementation_name=sys.implementation.name, implementation_version=iv,
    os_name=os.name, platform_machine=platform.machine(), platform_release=platform.release(),
    platform_system=platform.system(), platform_version=platform.version(),
    python_full_version=platform.python_version(), platform_python_implementation=platform.python_implementation(),
    python_version='.'.join(platform.python_version_tuple()[:2]), sys_platform=sys.platform)
print(json.dumps(dict(python=platform.python_version(), prefix=sys.prefix,
    base_prefix=sys.base_prefix, executable=sys.executable, markers=markers,
    site_packages=str(site), packages=packages)))
"""


def interpreter(path: Path) -> Path:
    return path / "bin" / "python"


def inspect(path: Path) -> dict:
    result = run([str(interpreter(path)), "-I", "-B", "-c", INSPECT])
    try:
        data = json.loads(result.stdout)
        names = [canonicalize_name(p["name"]) for p in data["packages"]]
        if len(names) != len(set(names)):
            raise ValueError("duplicate installed distributions")
        if Path(data["prefix"]).resolve() != path.resolve():
            raise ValueError("interpreter resolves to another environment")
        for package in data["packages"]:
            direct = package["direct_url"]
            if direct:
                url = urlsplit(direct["url"])
                if url.username or url.password:
                    raise ValueError(
                        f"credential-bearing source URL for {package['name']}; sanitize its metadata first"
                    )
        data["packages"].sort(key=lambda p: canonicalize_name(p["name"]))
        annotate_conda_ownership(path, data)
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise AWMError(f"Cannot inspect environment {path}: {exc}") from exc


def annotate_conda_ownership(path: Path, snapshot: dict) -> None:
    """Conda builds may use pip and retain build-host URLs; INSTALLER alone is insufficient."""
    owners = {}
    for record_path in (path / "conda-meta").glob("*.json"):
        record = json.loads(record_path.read_text())
        hashes = {}
        for entry in record.get("paths_data", {}).get("paths", []):
            relative = entry["_path"]
            if relative.startswith("site-packages/"):
                relative = str(
                    Path(snapshot["site_packages"]).relative_to(path)
                    / relative[len("site-packages/") :]
                )
            hashes[relative] = entry.get("sha256_in_prefix") or entry.get("sha256")
        for filename in record.get("files", []):
            if filename.endswith((".dist-info/METADATA", ".egg-info/PKG-INFO")):
                owners[str(path / filename)] = (set(record["files"]), hashes)
    for package in snapshot["packages"]:
        package["conda_owned"] = False
        metadata = package.get("metadata_path")
        if not metadata or metadata not in owners:
            continue
        files, hashes = owners[metadata]
        matched = True
        for filename in (Path(metadata).name, "INSTALLER", "direct_url.json"):
            current = Path(metadata).parent / filename
            relative = str(current.relative_to(path))
            if current.exists() != (relative in files):
                matched = False
                break
            expected = hashes.get(relative)
            if (
                current.exists()
                and expected
                and hashlib.sha256(current.read_bytes()).hexdigest() != expected
            ):
                matched = False
                break
        package["conda_owned"] = matched


def editable_path(package: dict) -> Path | None:
    direct = package.get("direct_url") or {}
    if not direct.get("dir_info", {}).get("editable"):
        return None
    url = urlsplit(direct["url"])
    if url.scheme != "file" or url.netloc not in ("", "localhost"):
        raise AWMError(f"Unsupported editable source: {package['name']}")
    return Path(unquote(url.path)).resolve()


def requirement(package: dict) -> str:
    direct = package.get("direct_url")
    if not direct:
        return f"{package['name']}=={package['version']}"
    url = direct["url"]
    vcs = direct.get("vcs_info")
    if vcs:
        commit = vcs.get("commit_id")
        if not commit:
            raise AWMError(f"Missing immutable VCS revision for {package['name']}")
        url = f"{vcs['vcs']}+{url}@{commit}"
    parsed = urlsplit(url)
    fragments = []
    if direct.get("subdirectory"):
        from urllib.parse import quote

        fragments.append("subdirectory=" + quote(direct["subdirectory"], safe="/"))
    hashes = direct.get("archive_info", {}).get("hashes", {})
    if "sha256" in hashes:
        fragments.append("sha256=" + hashes["sha256"])
    if fragments:
        url = urlunsplit(parsed._replace(fragment="&".join(fragments)))
    if parsed.scheme == "file" and not Path(unquote(parsed.path)).exists():
        raise AWMError(f"Source artifact is unavailable for {package['name']}: {parsed.path}")
    return f"{package['name']} @ {url}"


def dependency_errors(snapshot: dict, extras: dict[str, set[str]] | None = None) -> list[str]:
    packages = {canonicalize_name(p["name"]): p for p in snapshot["packages"]}
    errors = []
    for name, package in packages.items():
        for text in package["requires"]:
            try:
                req = Requirement(text)
            except InvalidRequirement as exc:
                errors.append(f"{name}: invalid dependency metadata: {exc}")
                continue
            active_extras = {"", *(extras or {}).get(name, set())}
            if req.marker and not any(
                req.marker.evaluate({**snapshot["markers"], "extra": extra})
                for extra in active_extras
            ):
                continue
            installed = packages.get(canonicalize_name(req.name))
            if installed is None or (
                req.specifier and not req.specifier.contains(installed["version"], prereleases=True)
            ):
                errors.append(
                    f"{name} requires {req}; installed: {installed['version'] if installed else 'missing'}"
                )
    return errors


def executable(kind: str) -> str:
    if kind == "venv":
        return sys.executable
    candidate = os.environ.get("CONDA_EXE") if kind == "conda" else None
    result = candidate if candidate and os.access(candidate, os.X_OK) else shutil.which(kind)
    if not result:
        raise AWMError(
            f"Selected backend '{kind}' is unavailable; install it or select another backend"
        )
    return result


def validate_base(path: Path, kind: str) -> dict:
    executable(kind)
    if kind == "conda":
        if not (path / "conda-meta").is_dir():
            raise AWMError(f"Not a conda environment: {path}")
    else:
        cfg = path / "pyvenv.cfg"
        if not cfg.is_file():
            raise AWMError("The base must be a dedicated virtual environment")
        values = dict(
            line.lower().split("=", 1) for line in cfg.read_text().splitlines() if "=" in line
        )
        if any(
            key.strip() == "include-system-site-packages" and value.strip() == "true"
            for key, value in values.items()
        ):
            raise AWMError("Bases that inherit system site-packages cannot be reproduced safely")
    snapshot = inspect(path)
    errors = dependency_errors(snapshot)
    if errors:
        raise AWMError("Base environment has inconsistent dependencies:\n" + "\n".join(errors))
    return snapshot


def create_environment(kind: str, base: Path, target: Path) -> None:
    if kind == "conda":
        args = [
            executable(kind),
            "create",
            "--yes",
            "--copy",
            "--clone",
            str(base),
            "--prefix",
            str(target),
        ]
    elif kind == "uv":
        args = [
            executable(kind),
            "venv",
            "--no-project",
            "--no-python-downloads",
            "--python",
            str(interpreter(base)),
            str(target),
        ]
    else:
        args = [str(interpreter(base)), "-I", "-B", "-m", "venv", "--without-pip", str(target)]
    run(args, timeout=1800)


def install(kind: str, target: Path, packages: list[str], editables: list[str]) -> None:
    if not packages and not editables:
        return
    if kind == "uv":
        args = [
            executable(kind),
            "pip",
            "install",
            "--python",
            str(interpreter(target)),
            "--no-deps",
            "--link-mode",
            "copy",
        ]
    else:
        args = [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--python",
            str(interpreter(target)),
            "install",
            "--no-deps",
            "--disable-pip-version-check",
        ]
    if kind == "conda":
        args.append("--force-reinstall")  # repair entry points of pip packages copied by conda
    for path in editables:
        args.extend(["--editable", path])
    args.extend(packages)
    environment = clean_env()
    # Installer destination defaults must not redirect a write into another environment.
    for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER", "PIP_PYTHON", "PIP_LOG"):
        environment.pop(key, None)
    environment["PIP_CONFIG_FILE"] = os.devnull
    run(args, timeout=1800, env=environment)


def conda_environments() -> list[Env]:
    data = json.loads(run([executable("conda"), "env", "list", "--json"]).stdout)
    return [Env(Path(p).name, str(Path(p).resolve()), "conda") for p in data.get("envs", [])]


def remove_environment(kind: str, path: Path) -> None:
    if kind == "conda":
        run([executable(kind), "env", "remove", "--yes", "--prefix", str(path)], timeout=1800)
        # Conda may leave untracked files. Do not claim a successful cleanup of them.
        if path.exists():
            if any(path.iterdir()):
                raise AWMError(f"Conda left files at {path}; inspect them before retrying cleanup")
            path.rmdir()
    else:
        shutil.rmtree(path)


def shell_command() -> list[str]:
    shell = shutil.which(os.environ.get("SHELL") or "/bin/sh")
    if not shell:
        raise AWMError("The configured SHELL is unavailable")
    options = {
        "bash": ["--noprofile", "--norc", "-i"],
        "zsh": ["-f", "-i"],
        "fish": ["--no-config", "-i"],
        "sh": ["-i"],
        "dash": ["-i"],
    }
    name = Path(shell).name
    if name not in options:
        raise AWMError("Set SHELL to bash, zsh, fish, sh or dash to open a sandbox shell")
    return [shell, *options[name]]


def run_environment(
    kind: str, path: Path, args: list[str], cwd: Path, *, interactive: bool = False
) -> int:
    import signal
    import subprocess

    env = clean_env()
    if interactive:
        # Startup scripts can auto-activate another environment; shells skip them.
        env.pop("ENV", None)
        env.pop("BASH_ENV", None)
        env["PS1"] = "(awm) $ "
    env["PATH"] = str(path / "bin") + os.pathsep + env.get("PATH", "")
    if kind == "conda":
        args = [executable(kind), "run", "--no-capture-output", "--prefix", str(path), *args]
    else:
        env["VIRTUAL_ENV"] = str(path)
    try:
        if interactive:
            terminal = sys.stdin.fileno() if sys.stdin.isatty() else None
            foreground = os.tcgetpgrp(terminal) if terminal is not None else None
            try:
                with subprocess.Popen(args, cwd=cwd, env=env) as process:
                    while True:
                        try:
                            return process.wait()
                        except KeyboardInterrupt:
                            # Ctrl-C also reaches the foreground shell. Keep its lock
                            # until it exits, rather than abandoning a live shell.
                            continue
            finally:
                if terminal is not None:
                    # Interactive shells take foreground ownership for job control.
                    # Restore terminal ownership before unwinding, without being stopped.
                    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                    try:
                        os.tcsetpgrp(terminal, foreground)
                    finally:
                        signal.signal(signal.SIGTTOU, previous)
        return subprocess.run(args, cwd=cwd, env=env).returncode
    except OSError as exc:
        raise AWMError(f"Cannot run command: {exc}") from exc
