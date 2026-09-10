"""Exercise release decisions and version writes without contacting a remote."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import git, make_repo
from packaging.version import Version

pytest.importorskip("semantic_release", reason="Install .[release] for release integration checks")

SOURCE = Path(__file__).resolve().parents[1]


def release_repo(tmp_path):
    repo = make_repo(tmp_path / "release")
    for relative in ("pyproject.toml", "agent_worktree_manager/__init__.py"):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE / relative).read_bytes())
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: initial release")
    version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    git(repo, "tag", f"v{version}")
    # No command below pushes, creates a remote release, or builds a package.
    git(repo, "remote", "add", "origin", "https://github.com/example/awm-release-test.git")
    return repo, Version(version)


@pytest.mark.parametrize(
    "message,bump",
    [
        ("feat(cli): add a command", "minor"),
        ("fix(env): correct an editable link", "patch"),
        ("perf(ui): speed up refresh", "patch"),
        ("docs: clarify setup", "none"),
        ("ci: update checks", "none"),
        ("feat!: change the configuration schema", "breaking"),
    ],
)
def test_semantic_release_updates_both_versions_and_tags(tmp_path, message, bump):
    repo, previous = release_repo(tmp_path)
    git(repo, "commit", "--allow-empty", "-m", message)
    before = git(repo, "rev-parse", "HEAD")
    output = tmp_path / "github-output"
    environment = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_COMMIT_AUTHOR"):
        environment.pop(name, None)
    environment["GITHUB_OUTPUT"] = str(output)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_release",
            "version",
            "--no-push",
            "--no-vcs-release",
            "--skip-build",
        ],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    if bump == "breaking" and previous.major:
        expected = f"{previous.major + 1}.0.0"
    elif bump in ("minor", "breaking"):
        expected = f"{previous.major}.{previous.minor + 1}.0"
    elif bump == "patch":
        expected = f"{previous.major}.{previous.minor}.{previous.micro + 1}"
    else:
        expected = str(previous)
    assert tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"] == expected
    runtime = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from agent_worktree_manager import __version__; import json; print(json.dumps(__version__))",
        ],
        cwd=repo,
        text=True,
    )
    assert json.loads(runtime) == expected
    assert f"v{expected}" in git(repo, "tag", "--list").splitlines()
    if bump == "none":
        assert git(repo, "rev-parse", "HEAD") == before
        assert "released=false" in output.read_text()
        assert not (repo / "CHANGELOG.md").exists()
    else:
        assert git(repo, "log", "-1", "--format=%s") == f"chore(release): {expected} [skip ci]"
        assert "released=true" in output.read_text()
        assert f"tag=v{expected}" in output.read_text()
        assert expected in (repo / "CHANGELOG.md").read_text()
