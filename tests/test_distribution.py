"""Exercise the installed wheel, not imports from the source checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_repo


@pytest.mark.integration
def test_installed_wheel_across_projects_and_uninstallation(tmp_path, monkeypatch):
    raw = os.environ.get("AWM_TEST_WHEELHOUSE")
    if not raw:
        pytest.skip("Set AWM_TEST_WHEELHOUSE to downloaded release/runtime wheels")
    wheelhouse = Path(raw).resolve()
    wheels = list(wheelhouse.glob("agent_worktree_manager-*.whl"))
    assert len(wheels) == 1
    tool = tmp_path / "installed tool"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tool)], check=True)
    installer = [sys.executable, "-m", "pip", "--python", str(tool / "bin/python")]
    subprocess.run(
        [*installer, "install", "--no-index", "--find-links", str(wheelhouse), str(wheels[0])],
        check=True,
        capture_output=True,
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    def awm(*args):
        return subprocess.run(
            [str(tool / "bin/awm"), *args],
            cwd=tmp_path,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    check = (
        "import agent_worktree_manager as a; from pathlib import Path; assert Path(a.__file__).is_relative_to("
        + repr(str(tool))
        + ")"
    )
    subprocess.run([str(tool / "bin/python"), "-I", "-c", check], cwd=tmp_path, check=True)
    skill = tmp_path / "installed skill"
    installed_version = awm("--version").strip()
    assert installed_version.startswith("awm ")
    exported = awm("skill")
    awm("skill", "--install", str(skill))
    assert (skill / "SKILL.md").read_text() == exported
    assert (skill / "agents/openai.yaml").is_file()
    interpreters = [sys.executable, os.environ.get("AWM_TEST_SECOND_PYTHON", sys.executable)]
    records = []
    for name, python in zip(("first", "second"), interpreters, strict=True):
        root = tmp_path / name
        root.mkdir()
        make_repo(root / "repo")
        base = tmp_path / (name + " base")
        subprocess.run([python, "-m", "venv", "--without-pip", str(base)], check=True)
        awm("init", "--root", str(root), "--base", str(base), "--name", name, "--repo", "repo=repo")
        awm("doctor", "--project", name)
        awm("create", "--project", name, "same")
        awm(
            "run", "--project", name, "same", "--", "python", "-c", "import sys; print(sys.version)"
        )
        records.append((root, base))
    result = json.loads(awm("list", "--all", "--json"))
    assert len(result["projects"]) == 2
    assert all(p["sandboxes"][0]["name"] == "same" for p in result["projects"])
    originals = [(root / ".awm/state.json").read_bytes() for root, _ in records]
    second_root = records[1][0]
    awm("projects", "remove", "second")
    assert (second_root / ".awm/state.json").read_bytes() == originals[1]
    assert (second_root / ".awm/sandboxes/same/env/bin/python").exists()
    awm("projects", "add", str(second_root))
    moved = second_root.with_name("temporarily unavailable")
    second_root.rename(moved)
    try:
        overview = json.loads(awm("projects", "list", "--json"))
        assert "unavailable" in next(p for p in overview["projects"] if p["name"] == "second")
    finally:
        moved.rename(second_root)
    registry_path = Path(environment["AWM_DATA_HOME"]) / "projects.json"
    registry_before = registry_path.read_bytes()
    subprocess.run(
        [*installer, "install", "--force-reinstall", "--no-deps", str(wheels[0])],
        check=True,
        capture_output=True,
    )
    assert awm("--version").strip() == installed_version
    subprocess.run(
        [*installer, "uninstall", "--yes", "agent-worktree-manager"],
        check=True,
        capture_output=True,
    )
    assert not (tool / "bin/awm").exists()
    assert (skill / "SKILL.md").read_text() == exported
    assert registry_path.read_bytes() == registry_before
    for (root, base), original in zip(records, originals, strict=True):
        assert (root / ".awm/state.json").read_bytes() == original
        assert (base / "bin/python").exists()
        assert (root / ".awm/sandboxes/same/env/bin/python").exists()
