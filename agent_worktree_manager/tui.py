"""Interactive curses picker for awm.

Shows every sandbox with metadata (worktrees, branches, dirty/unpushed state,
last commit, disk sizes) and lets you check/uncheck what to delete. Confirmed
selections are handed to the same plan/confirm/delete pipeline as
``awm delete``, so all its safety rules still apply.
"""

from __future__ import annotations

import argparse
import curses
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_worktree_manager import cli


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
@dataclass
class WtInfo:
    wt: cli.Worktree
    dirty: int = 0
    last_age: str = "?"
    last_subject: str = ""
    unpushed: str = ""
    size_kb: int | None = None  # filled by the background du thread


@dataclass
class Item:
    name: str
    env_name: str | None = None
    env_path: str | None = None
    wts: list[WtInfo] = field(default_factory=list)
    env_size_kb: int | None = None
    selected: bool = False

    @property
    def dirty(self) -> bool:
        return any(w.dirty for w in self.wts)

    @property
    def total_kb(self) -> int:
        return sum(filter(None, [self.env_size_kb, *(w.size_kb for w in self.wts)]))


def human_age(ts: float) -> str:
    delta = max(0, time.time() - ts)
    for unit, sec in (("y", 31536000), ("mo", 2592000), ("w", 604800), ("d", 86400), ("h", 3600)):
        if delta >= sec:
            return f"{int(delta // sec)}{unit}"
    return "<1h"


def human_size(kb: int | None) -> str:
    if kb is None:
        return "…"
    if kb >= 1 << 20:
        return f"{kb / (1 << 20):.1f}G"
    if kb >= 1 << 10:
        return f"{kb / (1 << 10):.0f}M"
    return f"{kb}K"


def wt_info(wt: cli.Worktree) -> WtInfo:
    info = WtInfo(wt=wt)
    if wt.prunable:
        info.last_subject = "(directory missing)"
        return info
    r = cli.git(wt.path, "status", "--porcelain")
    if r.returncode == 0:
        info.dirty = len([line for line in r.stdout.splitlines() if line.strip()])
    r = cli.git(wt.path, "log", "-1", "--format=%ct%x00%s")
    if r.returncode == 0 and r.stdout.strip():
        ts, _, subject = r.stdout.strip().partition("\x00")
        try:
            info.last_age = human_age(int(ts))
        except ValueError:
            pass
        info.last_subject = subject
    r = cli.git(wt.path, "rev-list", "--count", "@{upstream}..HEAD")
    if r.returncode == 0:
        n = int(r.stdout.strip() or 0)
        info.unpushed = f"{n} unpushed" if n else "pushed"
    else:
        info.unpushed = "no upstream"
    return info


def gather(root: Path, base_env: str) -> tuple[list[Item], str | None]:
    """All sandboxes under *root* with per-worktree metadata, plus the conda exe."""
    items: dict[str, Item] = {}
    for repo in cli.find_repos(root):
        prefix = f"{repo.name}_"
        for wt in cli.worktrees_of(repo):
            if wt.path.name.startswith(prefix) and len(wt.path.name) > len(prefix):
                name = wt.path.name[len(prefix) :]
                items.setdefault(name, Item(name=name)).wts.append(wt_info(wt))

    conda = cli.find_conda()
    envs = cli.conda_envs(conda) if conda else {}
    env_prefix = f"{base_env}_"
    for env_name, path in envs.items():
        if env_name.startswith(env_prefix) and len(env_name) > len(env_prefix):
            name = env_name[len(env_prefix) :]
            item = items.setdefault(name, Item(name=name))
            item.env_name, item.env_path = env_name, path

    return [items[k] for k in sorted(items)], conda


def _du_kb(path: str | Path) -> int | None:
    r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def start_size_thread(items: list[Item]) -> None:
    def work() -> None:
        for item in items:
            if item.env_path:
                item.env_size_kb = _du_kb(item.env_path)
            for w in item.wts:
                if not w.wt.prunable:
                    w.size_kb = _du_kb(w.wt.path)

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------
KEY_HELP = "[space] toggle  [a] all  [enter] delete selected  [q] quit"


def _safe_add(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w - 1:
        return
    try:
        stdscr.addstr(y, x, text[: w - 1 - x], attr)
    except curses.error:
        pass


def _wt_summary(item: Item) -> str:
    parts = []
    for w in item.wts:
        marker = " (missing)" if w.wt.prunable else ("*" if w.dirty else "")
        parts.append(f"{w.wt.repo.name}[{w.wt.branch or 'detached'}]{marker}")
    return ", ".join(parts) or "-"


def _draw(
    stdscr, items: list[Item], idx: int, top: int, state: dict, root: Path, base_env: str
) -> int:
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    bold = curses.A_BOLD
    dim = curses.A_DIM
    _safe_add(
        stdscr, 0, 0, f" awm — {len(items)} sandboxes in {root}  (base env: {base_env})", bold
    )
    toggles = (
        f"[b] delete branches: {'YES' if state['delete_branch'] else 'no'}   "
        f"[f] force (discard dirty): {'YES' if state['force'] else 'no'}   "
        f"[e] keep envs: {'YES' if state['keep_env'] else 'no'}   "
        f"[w] keep worktrees: {'YES' if state['keep_worktrees'] else 'no'}"
    )
    _safe_add(stdscr, 1, 0, f" {KEY_HELP}", dim)
    _safe_add(stdscr, 2, 0, f" {toggles}", dim)

    current = items[idx]
    detail_h = 2 + max(1, len(current.wts)) + (1 if current.env_name else 1)
    list_h = max(1, h - 4 - detail_h - 1)
    if idx < top:
        top = idx
    if idx >= top + list_h:
        top = idx - list_h + 1

    name_w = min(max((len(i.name) for i in items), default=4) + 2, 28)
    for row, item in enumerate(items[top : top + list_h]):
        y = 4 + row
        is_cur = (top + row) == idx
        box = "[x]" if item.selected else "[ ]"
        env_sz = human_size(item.env_size_kb) if item.env_path else "-"
        line = f" {'>' if is_cur else ' '} {box} {item.name:<{name_w}} env {env_sz:>6}  {_wt_summary(item)}"
        _safe_add(stdscr, y, 0, line, curses.A_REVERSE if is_cur else 0)

    sep_y = 4 + list_h
    _safe_add(stdscr, sep_y, 0, "─" * (w - 1), dim)
    y = sep_y + 1
    n_sel = sum(1 for i in items if i.selected)
    total = sum(i.total_kb for i in items if i.selected)
    pending = any(
        (i.env_path and i.env_size_kb is None)
        or any(w.size_kb is None and not w.wt.prunable for w in i.wts)
        for i in items
        if i.selected
    )
    total_str = f"~{human_size(total)}{'+…' if pending else ''}" if n_sel else "0K"
    _safe_add(stdscr, y, 1, f"{current.name}", bold)
    _safe_add(stdscr, y, 2 + len(current.name), f"    selected: {n_sel}  ({total_str})", dim)
    y += 1
    if current.env_name and current.env_path:
        _safe_add(
            stdscr,
            y,
            1,
            f"env  {current.env_name}  {human_size(current.env_size_kb):>6}  {current.env_path}",
        )
    else:
        _safe_add(stdscr, y, 1, "env  (none)", dim)
    y += 1
    if not current.wts:
        _safe_add(stdscr, y, 1, "wt   (none)", dim)
    for wi in current.wts:
        bits = [
            f"wt   {wi.wt.path.name}  [{wi.wt.branch or 'detached'}]  {human_size(wi.size_kb):>6}"
        ]
        if wi.wt.prunable:
            bits.append("directory missing")
        else:
            bits.append(f"{wi.dirty} dirty" if wi.dirty else "clean")
            bits.append(wi.unpushed)
            bits.append(f"last {wi.last_age}: {wi.last_subject}")
        _safe_add(stdscr, y, 1, "  ".join(bits))
        y += 1

    stdscr.refresh()
    return top


def run_ui(stdscr, items: list[Item], root: Path, base_env: str, state: dict) -> bool:
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # some terminals (e.g. vt100) can't hide the cursor
    stdscr.timeout(250)  # wake up regularly so background du results appear
    idx, top = 0, 0
    while True:
        top = _draw(stdscr, items, idx, top, state, root, base_env)
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = min(len(items) - 1, idx + 1)
        elif ch == ord(" "):
            items[idx].selected = not items[idx].selected
        elif ch == ord("a"):
            target = not all(i.selected for i in items)
            for i in items:
                i.selected = target
        elif ch == ord("b"):
            state["delete_branch"] = not state["delete_branch"]
        elif ch == ord("f"):
            state["force"] = not state["force"]
        elif ch == ord("e"):
            state["keep_env"] = not state["keep_env"]
        elif ch == ord("w"):
            state["keep_worktrees"] = not state["keep_worktrees"]
        elif ch in (ord("q"), 27):
            return False
        elif ch in (curses.KEY_ENTER, 10, 13, ord("d")):
            if any(i.selected for i in items):
                return True


# ---------------------------------------------------------------------------
# Entry point (wired from cli.cmd_ui)
# ---------------------------------------------------------------------------
def cmd_ui(opts: argparse.Namespace) -> int:
    import sys

    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        cli.err("interactive mode needs a TTY — use 'awm delete NAME…' instead")
        return 2

    root = Path(opts.root).expanduser().resolve() if opts.root else cli.default_root()
    if not cli.find_repos(root):
        cli.err(f"no git repos found under {root} (use --root to point at the workspace)")
        return 2

    cli.log(f"gathering sandboxes under {root}…")
    items, _conda = gather(root, opts.base_env)
    if not items:
        cli.log("no sandboxes found")
        return 0
    start_size_thread(items)

    state = {"force": False, "delete_branch": False, "keep_env": False, "keep_worktrees": False}
    proceed = curses.wrapper(run_ui, items, root, opts.base_env, state)
    if not proceed:
        cli.log("nothing deleted")
        return 0

    ns = argparse.Namespace(
        names=[i.name for i in items if i.selected],
        root=str(root),
        base_env=opts.base_env,
        dry_run=False,
        force=state["force"],
        yes=False,  # cmd_delete shows the plan and asks for a final y/N
        keep_env=state["keep_env"],
        keep_worktrees=state["keep_worktrees"],
        delete_branch=state["delete_branch"],
    )
    return cli.cmd_delete(ns)
