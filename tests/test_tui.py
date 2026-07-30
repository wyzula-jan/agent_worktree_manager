import subprocess
import time
from pathlib import Path

import pytest

from agent_worktree_manager import cli, tui


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / cli.ROOT_MARKER).write_text("")
    repo = tmp_path / "demo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init commit", cwd=repo)
    monkeypatch.setattr(cli, "find_conda", lambda: None)
    monkeypatch.setattr(cli, "conda_envs", lambda conda: {})
    return tmp_path, repo


def test_human_size():
    assert tui.human_size(None) == "…"
    assert tui.human_size(512) == "512K"
    assert tui.human_size(2048) == "2M"
    assert tui.human_size(3 << 20) == "3.0G"


def test_human_age():
    now = time.time()
    assert tui.human_age(now) == "<1h"
    assert tui.human_age(now - 90000) == "1d"
    assert tui.human_age(now - 3 * 604800) == "3w"


def test_gather_collects_metadata(workspace):
    root, repo = workspace
    wt = root / "demo_t1"
    _git("worktree", "add", "-b", "t1", str(wt), "main", cwd=repo)
    (wt / "dirty.txt").write_text("x\n")

    items, conda = tui.gather(root, "bec_312")
    assert conda is None
    assert [i.name for i in items] == ["t1"]
    item = items[0]
    assert item.env_name is None
    assert len(item.wts) == 1
    wi = item.wts[0]
    assert wi.wt.branch == "t1"
    assert wi.dirty == 1
    assert wi.last_subject == "init commit"
    assert wi.unpushed == "no upstream"
    assert item.dirty


def test_gather_env_only(workspace, monkeypatch):
    root, _ = workspace
    monkeypatch.setattr(cli, "find_conda", lambda: "/fake/conda")
    monkeypatch.setattr(
        cli, "conda_envs", lambda conda: {"bec_312_solo": "/fake/envs/bec_312_solo"}
    )
    items, _ = tui.gather(root, "bec_312")
    assert [i.name for i in items] == ["solo"]
    assert items[0].env_name == "bec_312_solo"
    assert items[0].wts == []


def test_du_kb(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"0" * 4096)
    kb = tui._du_kb(tmp_path)
    assert isinstance(kb, int) and kb > 0
    assert tui._du_kb(tmp_path / "missing") is None
