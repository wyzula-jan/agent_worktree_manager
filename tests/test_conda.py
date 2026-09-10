import os
from pathlib import Path

import pytest

from agent_worktree_manager import environments, lifecycle


@pytest.mark.conda
@pytest.mark.integration
def test_native_conda_roundtrip(make_workspace, install_package, tmp_path):
    reference = os.environ.get("AWM_TEST_CONDA_BASE")
    if not reference:
        pytest.skip("Set AWM_TEST_CONDA_BASE to a reference conda environment")
    base = tmp_path / "conda-base"
    base.mkdir()
    environments.create_environment("conda", Path(reference), base)
    try:
        project, repo = make_workspace("conda-project", backend="conda", base=base)
        install_package(base, repo)
        install_package(base, tmp_path / "dependency", name="regular_dep", editable=False)
        before = environments.inspect(base)
        record = lifecycle.create(project, "feature", [])
        assert record["status"] == "ready"
        assert record["editables"][0]["shared"] is False
        assert lifecycle.run_command(project, "feature", ["demo_pkg-where"]) == 0
        assert lifecycle.run_command(project, "feature", ["regular_dep-where"]) == 0
        script = Path(record["environments"][0]["path"]) / "bin/regular_dep-where"
        assert str(base) not in script.read_text()
        assert environments.inspect(base) == before
        lifecycle.delete(project, ["feature"])
        assert not Path(record["environments"][0]["path"]).exists()
        assert base.exists()
    finally:
        environments.remove_environment("conda", base)
