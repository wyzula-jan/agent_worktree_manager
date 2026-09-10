import time

from conftest import make_venv
from test_cli import add_worktree

from agent_worktree_manager import inspection, lifecycle, migration, tui


def test_human_size():
    assert inspection.human_size(None) == "…"
    assert inspection.human_size(0) == "0K"
    assert inspection.human_size(2048) == "2M"
    assert inspection.human_size(1 << 20) == "1.0G"


def test_human_age():
    assert inspection.human_age(time.time()) == "<1h"
    assert inspection.human_age(time.time() - 86400 * 2) == "2d"


def test_gather_collects_metadata(workspace):
    project, repo = workspace
    path = add_worktree(project, repo)
    (path / "untracked").write_text("dirty")
    item = inspection.gather(project)[0]
    assert item.name == "t1" and item.wts[0].dirty == 1
    assert item.wts[0].last_subject == "initial"
    assert item.wts[0].unpushed == "no upstream"


def test_gather_env_only(workspace, tmp_path):
    project, _ = workspace
    env = make_venv(tmp_path / "env")
    migration.import_sandbox(project, "envonly", [], [f"venv={env}"])
    item = inspection.gather(project)[0]
    assert item.envs[0].path == str(env) and not item.wts


def test_gather_finds_uv_and_conda_envs(workspace, tmp_path):
    project, _ = workspace
    uv = make_venv(tmp_path / "uv")
    conda = make_venv(tmp_path / "conda")
    (conda / "conda-meta").mkdir()
    migration.import_sandbox(project, "mixed", [], [f"uv={uv}", f"conda={conda}"])
    assert {e.kind for e in inspection.gather(project)[0].envs} == {"uv", "conda"}


def test_du_kb(tmp_path):
    (tmp_path / "file").write_bytes(b"x" * 8192)
    assert inspection._du_kb(tmp_path) >= 8


class Screen:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.lines = []

    def getmaxyx(self):
        return (28, 140)

    def erase(self):
        pass

    def refresh(self):
        pass

    def timeout(self, value):
        pass

    def getch(self):
        return next(self.keys)

    def addstr(self, y, x, text, attr=0):
        self.lines.append(text)


def test_project_picker_keeps_unavailable_projects_visible(workspace, monkeypatch):
    project, _ = workspace
    (project.local / "local.toml").unlink()
    monkeypatch.setattr(tui.curses, "curs_set", lambda v: None)
    screen = Screen([ord("q")])
    assert tui.project_picker(screen) is None
    assert any("unavailable" in line for line in screen.lines)


def test_project_picker_opens_registered_project(workspace, monkeypatch):
    project, _ = workspace
    monkeypatch.setattr(tui.curses, "curs_set", lambda v: None)
    assert tui.project_picker(Screen([10])) == project.id


def test_app_displays_failure_and_shared_source(workspace):
    import argparse

    project, _ = workspace
    state = lifecycle.read_state(project)
    record = lifecycle.sandbox_record("failed")
    record.update(
        status="failed",
        error="package unavailable",
        editables=[dict(name="shared", path="/shared/source", shared=True)],
    )
    state["sandboxes"]["failed"] = record
    lifecycle.save_state(project, state)
    screen = Screen()
    app = tui.App(screen, argparse.Namespace(), project, "sandboxes")
    app.draw()
    assert any("package unavailable" in line for line in screen.lines)
    assert any("shared/source" in line for line in screen.lines)
