from pathlib import Path

import pytest

from agent_worktree_manager import cli
from agent_worktree_manager.agent_skill import bundled_files, install_skill
from agent_worktree_manager.errors import AWMError


def test_skill_export_and_install_without_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["skill"]) == 0
    exported = capsys.readouterr().out
    assert exported.encode() == bundled_files()["SKILL.md"]
    destination = tmp_path / "agent skills" / "awm"
    assert cli.main(["skill", "--install", str(destination)]) == 0
    assert (destination / "SKILL.md").read_text() == exported
    assert (destination / "agents/openai.yaml").read_bytes() == bundled_files()[
        "agents/openai.yaml"
    ]
    assert not (tmp_path / ".awm").exists()
    assert not list(destination.parent.glob(".awm-skill-*"))


def test_skill_install_is_idempotent_and_preserves_extra_files(tmp_path):
    destination = install_skill(tmp_path / "awm")
    extra = destination / "notes.txt"
    extra.write_text("personal notes")
    original = (destination / "SKILL.md").stat().st_mtime_ns
    assert install_skill(destination) == destination
    assert extra.read_text() == "personal notes"
    assert (destination / "SKILL.md").stat().st_mtime_ns == original


@pytest.mark.parametrize("existing", ["customized", "empty", "file", "symlink"])
def test_skill_install_preserves_existing_destinations(tmp_path, existing):
    destination = tmp_path / "awm"
    if existing == "file":
        destination.write_text("keep file")
    elif existing == "symlink":
        destination.symlink_to(tmp_path / "missing", target_is_directory=True)
    else:
        destination.mkdir()
        if existing == "customized":
            (destination / "SKILL.md").write_text("custom instructions")
    with pytest.raises(AWMError):
        install_skill(destination)
    if existing == "customized":
        assert (destination / "SKILL.md").read_text() == "custom instructions"
    elif existing == "empty":
        assert not list(destination.iterdir())
    elif existing == "file":
        assert destination.read_text() == "keep file"
    else:
        assert destination.is_symlink()


def test_skill_install_cleans_temporary_files_after_write_failure(tmp_path, monkeypatch):
    original = Path.write_bytes

    def fail_metadata(self, data):
        if self.name == "openai.yaml":
            raise OSError("write failed")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_metadata)
    with pytest.raises(OSError, match="write failed"):
        install_skill(tmp_path / "awm")
    assert not list(tmp_path.iterdir())
