"""Curses project overview and sandbox/environment views."""

from __future__ import annotations

import argparse
import curses
import os
import sys
import threading
import time
from dataclasses import dataclass

from . import cli, lifecycle, registry
from .config import Project
from .environments import Env
from .errors import AWMError
from .inspection import (
    SIZES,
    EnvItem,
    Item,
    Pkg,
    SizeWorker,
    _matches,
    gather,
    gather_envs,
    human_age,
    human_size,
    list_packages,
    make_env_item,
)

MODES = ("sandboxes", "envs")


@dataclass
class View:
    idx: int = 0
    top: int = 0
    filter: str = ""
    editing: bool = False


@dataclass
class PkgOverlay:
    env: EnvItem
    filter: str = ""
    editing: bool = False
    scroll: int = 0

    @property
    def visible(self) -> list[Pkg]:
        pkgs = self.env.pkgs or []
        return [p for p in pkgs if _matches(p.filter_text, self.filter)] if self.filter else pkgs


HELP = {
    "sandboxes": "[s] shell  [space] toggle  [a] all  [enter] delete  [p] packages  [/] filter  [tab] envs  [q] quit",
    "envs": "[space] toggle  [a] all  [enter] delete  [p] packages  [/] filter  [tab] sandboxes  [q] quit",
}
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)
ESC = 27
TAB = 9


def _safe_add(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w - 1:
        return
    try:
        win.addstr(y, x, text[: w - 1 - x], attr)
    except curses.error:
        pass


def _wt_summary(item: Item) -> str:
    parts = []
    for w in item.wts:
        marker = " (missing)" if w.wt.prunable else ("*" if w.dirty else "")
        parts.append(f"{w.wt.repo.name}[{w.wt.branch or 'detached'}]{marker}")
    return ", ".join(parts) or "-"


class App:
    def __init__(self, stdscr, opts: argparse.Namespace, project: Project, start_mode: str) -> None:
        self.scr = stdscr
        self.opts = opts
        self.project = project
        self.root = project.root
        self.mode = start_mode
        self.sb_items: list[Item] | None = None
        self.env_items: list[EnvItem] | None = None
        self.env_cache: dict[str, EnvItem] = {}
        self.views = {m: View() for m in MODES}
        self.toggles = {
            "force": False,
            "delete_branch": False,
            "keep_env": False,
            "keep_worktrees": False,
        }
        self.overlay: PkgOverlay | None = None
        self.sizes = SizeWorker()
        self.msg = ""
        self.msg_until = 0.0

    # -- data -------------------------------------------------------------
    def flash(self, msg: str, seconds: float = 3.0) -> None:
        self.msg, self.msg_until = msg, time.time() + seconds

    def _loading(self, what: str) -> None:
        self.scr.erase()
        _safe_add(self.scr, 0, 1, f"awm — gathering {what}…", curses.A_BOLD)
        self.scr.refresh()

    def ensure_loaded(self, mode: str) -> None:
        if mode == "sandboxes" and self.sb_items is None:
            self._loading("sandboxes (git status/log per worktree)")
            self.sb_items = gather(self.project)
            paths = [e.path for i in self.sb_items for e in i.envs]
            paths += [str(w.wt.path) for i in self.sb_items for w in i.wts if not w.wt.prunable]
            self.sizes.add(paths)
        elif mode == "envs" and self.env_items is None:
            self._loading("environments")
            self.env_items = gather_envs(self.project)
            for e in self.env_items:
                self.env_cache[e.env.path] = e
            self.sizes.add([e.env.path for e in self.env_items])

    def env_item_for(self, env: Env) -> EnvItem:
        item = self.env_cache.get(env.path)
        if item is None:
            item = make_env_item(self.project, env)
            self.env_cache[env.path] = item
        return item

    def items(self) -> list:
        self.ensure_loaded(self.mode)
        return self.sb_items if self.mode == "sandboxes" else self.env_items  # type: ignore[return-value]

    def visible(self) -> list:
        view = self.views[self.mode]
        items = self.items()
        if not view.filter:
            return items
        return [i for i in items if _matches(i.filter_text, view.filter)]

    def current(self):
        vis = self.visible()
        view = self.views[self.mode]
        if not vis:
            return None
        view.idx = max(0, min(view.idx, len(vis) - 1))
        return vis[view.idx]

    def _prioritize_current(self) -> None:
        cur = self.current()
        if cur is None:
            return
        if isinstance(cur, EnvItem):
            self.sizes.prioritize(cur.env.path)
        elif cur.envs:
            self.sizes.prioritize(cur.envs[0].path)
        elif cur.wts:
            self.sizes.prioritize(cur.wts[0].wt.path)

    # -- packages overlay ---------------------------------------------------
    def open_packages(self, env_item: EnvItem) -> None:
        self.overlay = PkgOverlay(env=env_item)
        if env_item.pkgs is None and not env_item.pkgs_loading and env_item.pkgs_error is None:
            env_item.pkgs_loading = True

            def work() -> None:
                pkgs, error = list_packages(env_item.interpreter)
                env_item.pkgs, env_item.pkgs_error = (pkgs if error is None else None), error
                env_item.pkgs_loading = False

            threading.Thread(target=work, daemon=True).start()

    def overlay_key(self, ch: int) -> None:
        ov = self.overlay
        assert ov is not None
        if ov.editing:
            if ch in ENTER_KEYS:
                ov.editing = False
            elif ch == ESC:
                ov.filter, ov.editing = "", False
            elif ch in BACKSPACE_KEYS:
                ov.filter = ov.filter[:-1]
            elif 32 <= ch < 127:
                ov.filter += chr(ch)
            ov.scroll = 0
            return
        if ch in (ord("q"), ESC):
            self.overlay = None
        elif ch == ord("/"):
            ov.editing = True
        elif ch == ord("c"):
            ov.filter, ov.scroll = "", 0
        elif ch in (curses.KEY_DOWN, ord("j")):
            ov.scroll += 1
        elif ch in (curses.KEY_UP, ord("k")):
            ov.scroll = max(0, ov.scroll - 1)
        elif ch in (curses.KEY_NPAGE, 4):  # PgDn / ctrl-d
            ov.scroll += self._overlay_rows()
        elif ch in (curses.KEY_PPAGE, 21):  # PgUp / ctrl-u
            ov.scroll = max(0, ov.scroll - self._overlay_rows())
        elif ch == ord("g"):
            ov.scroll = 0
        elif ch == ord("G"):
            ov.scroll = max(0, len(ov.visible) - self._overlay_rows())
        elif ch == ord("r") and not ov.env.pkgs_loading:
            ov.env.pkgs, ov.env.pkgs_error = None, None
            self.open_packages(ov.env)

    def _overlay_rows(self) -> int:
        h, _ = self.scr.getmaxyx()
        return max(1, h - 2 - 2 - 4)  # box margins, title+filter, separator+footer

    # -- main key handling --------------------------------------------------
    def filter_key(self, view: View, ch: int) -> None:
        if ch in ENTER_KEYS:
            view.editing = False
        elif ch == ESC:
            view.filter, view.editing = "", False
        elif ch in BACKSPACE_KEYS:
            view.filter = view.filter[:-1]
        elif 32 <= ch < 127:
            view.filter += chr(ch)
        view.idx = 0
        self._prioritize_current()

    def toggle_current(self) -> None:
        cur = self.current()
        if cur is None:
            return
        if isinstance(cur, EnvItem) and cur.protected:
            self.flash(f"'{cur.name}' is protected ({cur.protected}) — cannot be selected")
            return
        cur.selected = not cur.selected

    def toggle_all(self) -> None:
        vis = [i for i in self.visible() if not (isinstance(i, EnvItem) and i.protected)]
        target = not all(i.selected for i in vis) if vis else False
        for i in vis:
            i.selected = target

    def handle_key(self, ch: int) -> str | None:
        """Return a terminal action, or None to keep browsing."""
        view = self.views[self.mode]
        if self.overlay is not None:
            self.overlay_key(ch)
            return None
        if view.editing:
            self.filter_key(view, ch)
            return None

        n = len(self.visible())
        page = max(1, self._list_rows())
        if ch in (curses.KEY_UP, ord("k")):
            view.idx = max(0, view.idx - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            view.idx = min(max(0, n - 1), view.idx + 1)
        elif ch == curses.KEY_PPAGE:
            view.idx = max(0, view.idx - page)
        elif ch == curses.KEY_NPAGE:
            view.idx = min(max(0, n - 1), view.idx + page)
        elif ch == ord("g"):
            view.idx = 0
        elif ch == ord("G"):
            view.idx = max(0, n - 1)
        elif ch == ord(" "):
            self.toggle_current()
        elif ch == ord("a"):
            self.toggle_all()
        elif ch == ord("s") and self.mode == "sandboxes":
            cur = self.current()
            if cur is None or not cur.envs:
                self.flash("this sandbox has no environment to activate")
            elif cur.status not in ("ready", "imported", "partial"):
                self.flash("this sandbox is not ready; inspect its failure first")
            else:
                return "activate"
        elif ch == ord("/"):
            view.editing = True
        elif ch == ord("r"):
            self.sb_items, self.env_items = None, None
            self.env_cache.clear()
            self.ensure_loaded(self.mode)
        elif ch == TAB:
            self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
            self.ensure_loaded(self.mode)
        elif ch == ord("p"):
            cur = self.current()
            if isinstance(cur, EnvItem):
                self.open_packages(cur)
            elif cur is not None and cur.envs:
                self.open_packages(self.env_item_for(cur.envs[0]))
            elif cur is not None:
                self.flash("this sandbox has no env")
        elif self.mode == "sandboxes" and ch == ord("b"):
            self.toggles["delete_branch"] = not self.toggles["delete_branch"]
        elif self.mode == "sandboxes" and ch == ord("f"):
            self.toggles["force"] = not self.toggles["force"]
        elif self.mode == "sandboxes" and ch == ord("e"):
            self.toggles["keep_env"] = not self.toggles["keep_env"]
        elif self.mode == "sandboxes" and ch == ord("w"):
            self.toggles["keep_worktrees"] = not self.toggles["keep_worktrees"]
        elif ch in (ord("q"), ESC):
            return "quit"
        elif ch in ENTER_KEYS or ch == ord("d"):
            if any(i.selected for i in self.items()):
                return "delete"
            self.flash("nothing selected — press space to check items first")
        self._prioritize_current()
        return None

    def selected_items(self) -> list:
        return [i for i in self.items() if i.selected]

    # -- drawing --------------------------------------------------------------
    def _detail_height(self, cur) -> int:
        if cur is None:
            return 1
        if isinstance(cur, EnvItem):
            return 4
        return (
            2 + max(1, len(cur.envs)) + max(1, len(cur.wts)) + len(cur.editables) + bool(cur.error)
        )

    def _list_rows(self) -> int:
        h, _ = self.scr.getmaxyx()
        return max(1, h - 6 - self._detail_height(self.current()))

    def draw(self) -> None:
        scr = self.scr
        h, w = scr.getmaxyx()
        scr.erase()
        bold, dim = curses.A_BOLD, curses.A_DIM

        # header: mode tabs
        x = 1
        _safe_add(scr, 0, x, "awm", bold)
        x += 5
        for m in MODES:
            count = ""
            if m == "sandboxes" and self.sb_items is not None:
                count = f" ({len(self.sb_items)})"
            if m == "envs" and self.env_items is not None:
                count = f" ({len(self.env_items)})"
            label = f" {m}{count} "
            _safe_add(scr, 0, x, label, curses.A_REVERSE if m == self.mode else dim)
            x += len(label) + 1
        _safe_add(scr, 0, x + 1, f"root: {self.root}", dim)
        _safe_add(scr, 1, 1, HELP[self.mode], dim)

        items = self.items()
        vis = self.visible()
        view = self.views[self.mode]
        cur = self.current()
        n_sel = len(self.selected_items())
        if self.mode == "sandboxes":
            t = self.toggles
            line2 = (
                f"[b] delete branches: {'YES' if t['delete_branch'] else 'no'}   "
                f"[f] force (discard dirty): {'YES' if t['force'] else 'no'}   "
                f"[e] keep envs: {'YES' if t['keep_env'] else 'no'}   "
                f"[w] keep worktrees: {'YES' if t['keep_worktrees'] else 'no'}"
            )
        else:
            line2 = f"base: {self.project.base} ({self.project.backend})   [r] refresh"
        _safe_add(scr, 2, 1, line2, dim)
        if view.filter or view.editing:
            cursor = "_" if view.editing else ""
            hint = "   (enter: apply, esc: clear)" if view.editing else ""
            _safe_add(
                scr, 3, 1, f"filter: {view.filter}{cursor}   {len(vis)}/{len(items)}{hint}", bold
            )

        # list
        list_h = self._list_rows()
        if view.idx < view.top:
            view.top = view.idx
        if view.idx >= view.top + list_h:
            view.top = view.idx - list_h + 1
        if not vis:
            _safe_add(scr, 4, 3, "no matches" if view.filter else "nothing here", dim)
        name_w = min(max((len(i.name) for i in vis), default=4) + 2, 36)
        for row, item in enumerate(vis[view.top : view.top + list_h]):
            y = 4 + row
            is_cur = (view.top + row) == view.idx
            line = self._row(item, name_w)
            attr = curses.A_REVERSE if is_cur else 0
            if isinstance(item, EnvItem) and item.protected and not is_cur:
                attr = dim
            _safe_add(scr, y, 0, f" {'>' if is_cur else ' '} {line}", attr)

        # detail pane
        sep_y = 4 + list_h
        _safe_add(scr, sep_y, 0, "─" * (w - 1), dim)
        if cur is not None:
            self._draw_detail(sep_y + 1, cur, n_sel)

        # message line
        if self.msg and time.time() < self.msg_until:
            _safe_add(scr, h - 1, 1, self.msg, curses.A_BOLD)

        if self.overlay is not None:
            self._draw_overlay()
        scr.refresh()

    def _row(self, item, name_w: int) -> str:
        if isinstance(item, EnvItem):
            box = "[-]" if item.protected else ("[x]" if item.selected else "[ ]")
            changed = human_age(item.changed) if item.changed else "?"
            sb = f"sandbox: {item.sandbox}" if item.sandbox else ""
            return (
                f"{box} {item.name:<{name_w}} {item.env.kind:<5} py {item.python:<8} "
                f"{human_size(item.size_kb):>6}  {changed:>4}  {sb}"
            )
        box = "[x]" if item.selected else "[ ]"
        env_sz = human_size(item.env_size_kb) if item.envs else "-"
        kinds = "/".join(e.kind for e in item.envs) or "-"
        return f"{box} {item.name:<{name_w}} {item.status:<9} {kinds:>8} {env_sz:>6}  {_wt_summary(item)}"

    def _draw_detail(self, y: int, cur, n_sel: int) -> None:
        scr = self.scr
        bold, dim = curses.A_BOLD, curses.A_DIM
        if isinstance(cur, EnvItem):
            selected = self.selected_items()
            total = sum(i.size_kb or 0 for i in selected)
            pending = any(i.size_kb is None for i in selected)
            total_str = f"~{human_size(total)}{'+…' if pending else ''}" if n_sel else "0K"
            label = f"{cur.name}  ({cur.env.kind})"
            _safe_add(scr, y, 1, label, bold)
            _safe_add(scr, y, 1 + len(label), f"    selected: {n_sel}  ({total_str})", dim)
            _safe_add(scr, y + 1, 1, f"path     {cur.env.path}")
            if cur.pkgs is not None:
                editable = sum(1 for p in cur.pkgs if p.editable)
                pk = f"{len(cur.pkgs)} packages" + (f" ({editable} editable)" if editable else "")
            elif cur.pkgs_loading:
                pk = "packages: loading…"
            elif cur.pkgs_error:
                pk = f"packages: {cur.pkgs_error}"
            else:
                pk = "packages: press [p]"
            changed = f"changed {human_age(cur.changed)} ago" if cur.changed else "changed ?"
            _safe_add(
                scr,
                y + 2,
                1,
                f"python   {cur.python:<10} size {human_size(cur.size_kb):<7} {changed:<18} {pk}",
            )
            bits = []
            if cur.sandbox:
                link = f"sandbox  {cur.sandbox}"
                if self.sb_items is not None:
                    match = next((i for i in self.sb_items if i.name == cur.sandbox), None)
                    if match is not None:
                        link += f"  worktrees: {_wt_summary(match)}"
                bits.append(link)
            if cur.protected:
                bits.append(f"protected: {cur.protected}")
            _safe_add(
                scr, y + 3, 1, "   ".join(bits) or "not a sandbox env", dim if not bits else 0
            )
            return

        total = sum(i.total_kb for i in self.selected_items())
        pending = any(i.sizes_pending for i in self.selected_items())
        total_str = f"~{human_size(total)}{'+…' if pending else ''}" if n_sel else "0K"
        _safe_add(scr, y, 1, cur.name, bold)
        _safe_add(scr, y, 2 + len(cur.name), f"    selected: {n_sel}  ({total_str})", dim)
        y += 1
        _safe_add(scr, y, 1, f"status: {cur.status}")
        y += 1
        if cur.error:
            _safe_add(scr, y, 1, f"error: {cur.error}", bold)
            y += 1
        for link in cur.editables:
            _safe_add(
                scr,
                y,
                1,
                f"{'shared' if link['shared'] else 'isolated'}  {link['name']} -> {link['path']}",
            )
            y += 1
        if cur.envs:
            for env in cur.envs:
                _safe_add(
                    scr, y, 1, f"env  {env.label}  {human_size(SIZES.get(env.path)):>6}  {env.path}"
                )
                y += 1
        else:
            _safe_add(scr, y, 1, "env  (none)", dim)
            y += 1
        if not cur.wts:
            _safe_add(scr, y, 1, "wt   (none)", dim)
        for wi in cur.wts:
            bits = [
                f"wt   {wi.wt.path.name}  [{wi.wt.branch or 'detached'}]  {human_size(wi.size_kb):>6}"
            ]
            if wi.wt.prunable:
                bits.append("directory missing")
            else:
                bits.append(f"{wi.dirty} dirty" if wi.dirty else "clean")
                bits.append(wi.unpushed)
                bits.append(f"last {wi.last_age}: {wi.last_subject}")
            _safe_add(scr, y, 1, "  ".join(bits))
            y += 1

    def _draw_overlay(self) -> None:
        """Package list box, painted over the main screen (top-left at (1, 2))."""
        ov = self.overlay
        assert ov is not None
        scr = self.scr
        h, w = scr.getmaxyx()
        y0, x0 = 1, 2
        bh, bw = max(6, h - 2), max(20, w - 4)
        inner = bw - 2

        def put(row: int, col: int, text: str, attr: int = 0) -> None:
            _safe_add(scr, y0 + row, x0 + col, text[: bw - col - 1], attr)

        put(0, 0, "┌" + "─" * inner + "┐")
        for row in range(1, bh - 1):
            put(row, 0, "│" + " " * inner + "│")
        put(bh - 1, 0, "└" + "─" * inner + "┘")

        env = ov.env
        vis = ov.visible
        total = len(env.pkgs or [])
        title = (
            f" packages in {env.name} ({env.env.kind}, python {env.python})  —  {len(vis)}/{total} "
        )
        put(0, 2, title, curses.A_BOLD)
        cursor = "_" if ov.editing else ""
        put(1, 2, f"/ {ov.filter}{cursor}", curses.A_BOLD if ov.editing else 0)
        help_text = "[/] filter  [c] clear  [j/k PgUp/PgDn g/G] scroll  [r] reload  [esc] close"
        put(1, max(2, inner - len(help_text)), help_text, curses.A_DIM)
        put(2, 1, "─" * inner, curses.A_DIM)

        rows = bh - 4
        if env.pkgs_loading:
            put(3, 2, "loading…  (running the env's interpreter)", curses.A_DIM)
        elif env.pkgs_error:
            put(3, 2, f"error: {env.pkgs_error}", curses.A_BOLD)
        elif not vis:
            put(3, 2, "no matches" if ov.filter else "no packages", curses.A_DIM)
        else:
            ov.scroll = max(0, min(ov.scroll, max(0, len(vis) - rows)))
            name_w = min(max(len(p.name) for p in vis) + 2, 40)
            ver_w = min(max(len(p.version) for p in vis) + 2, 24)
            for i, pkg in enumerate(vis[ov.scroll : ov.scroll + rows]):
                line = f"{pkg.name:<{name_w}}{pkg.version:<{ver_w}}"
                if pkg.editable:
                    line += f"-> {pkg.editable}"
                put(3 + i, 2, line, curses.A_BOLD if pkg.editable else 0)
            if len(vis) > rows:
                pos = f" {ov.scroll + 1}-{min(len(vis), ov.scroll + rows)} of {len(vis)} "
                put(bh - 1, inner - len(pos) - 1, pos, curses.A_DIM)

    # -- loop -----------------------------------------------------------------
    def run(self) -> str:
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # some terminals (e.g. vt100) can't hide the cursor
        self.scr.timeout(250)  # wake up regularly so background results appear
        self.ensure_loaded(self.mode)
        self._prioritize_current()
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch in (-1, curses.KEY_RESIZE):
                continue
            result = self.handle_key(ch)
            if result is not None:
                return result


def project_picker(scr) -> str | None:
    """Registered locations remain visible even if their disk is unavailable."""
    idx, query, editing, message = 0, "", False, ""
    rows = cli.project_overview()
    scr.timeout(250)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    while True:
        visible = [r for r in rows if _matches(f"{r['name']} {r['root']} {r['id']}", query)]
        idx = max(0, min(idx, len(visible) - 1))
        scr.erase()
        h, w = scr.getmaxyx()
        _safe_add(scr, 0, 1, "awm — registered projects", curses.A_BOLD)
        _safe_add(scr, 1, 1, "[enter] open  [/] filter  [r] refresh  [q] quit", curses.A_DIM)
        _safe_add(scr, 2, 1, f"filter: {query}{'_' if editing else ''}")
        page = max(1, h - 6)
        top = max(0, idx - page + 1)
        for n, row in enumerate(visible[top : top + page], start=top):
            status = (
                "unavailable"
                if "unavailable" in row
                else f"{row['sandboxes']} sandboxes {row['states']}"
            )
            _safe_add(
                scr,
                3 + n - top,
                1,
                f"{row['name']}  {status}  {row['root']}",
                curses.A_REVERSE if n == idx else 0,
            )
        if not rows:
            _safe_add(scr, 4, 1, "No projects registered. Use awm init or awm projects add PATH.")
        if visible:
            _safe_add(scr, h - 2, 1, visible[idx].get("unavailable", visible[idx]["id"]))
        _safe_add(scr, h - 1, 1, message)
        scr.refresh()
        key = scr.getch()
        if editing:
            if key in ENTER_KEYS:
                editing = False
            elif key == ESC:
                query, editing = "", False
            elif key in BACKSPACE_KEYS:
                query = query[:-1]
            elif 32 <= key < 127:
                query += chr(key)
            idx = 0
        elif key in (ord("q"), ESC):
            return None
        elif key in (curses.KEY_DOWN, ord("j")):
            idx += 1
        elif key in (curses.KEY_UP, ord("k")):
            idx -= 1
        elif key == ord("/"):
            editing = True
        elif key == ord("r"):
            rows = cli.project_overview()
        elif key in ENTER_KEYS and visible:
            if "unavailable" in visible[idx]:
                message = "Project unavailable; restore its location or unregister it."
            else:
                return visible[idx]["id"]


def choose_shell_target(title: str, labels: list[str]) -> int | None:
    if len(labels) == 1:
        return 0

    def picker(scr):
        index = 0
        while True:
            scr.erase()
            height, _ = scr.getmaxyx()
            _safe_add(scr, 0, 1, title, curses.A_BOLD)
            _safe_add(scr, 1, 1, "[enter] choose  [j/k] navigate  [esc/q] cancel")
            page = max(1, height - 3)
            top = max(0, index - page + 1)
            for row, label in enumerate(labels[top : top + page], start=top):
                _safe_add(scr, row - top + 2, 1, label, curses.A_REVERSE if row == index else 0)
            scr.refresh()
            key = scr.getch()
            if key in (ESC, ord("q")):
                return None
            if key in ENTER_KEYS:
                return index
            if key in (curses.KEY_DOWN, ord("j")):
                index = min(len(labels) - 1, index + 1)
            elif key in (curses.KEY_UP, ord("k")):
                index = max(0, index - 1)

    return curses.wrapper(picker)


def activate_current(app: App) -> None:
    item = app.current()
    if item is None:
        return
    record = lifecycle.read_state(app.project)["sandboxes"].get(item.name)
    if record is None or not record["environments"]:
        raise AWMError("Sandbox no longer has an environment")
    environments = record["environments"]
    index = choose_shell_target(
        f"Activate {item.name}: choose environment",
        [f"{e['kind']}  {e['path']}" for e in environments],
    )
    if index is None:
        return
    environment = environments[index]["path"]
    repo = None
    if record["worktrees"]:
        worktrees = record["worktrees"]
        index = choose_shell_target(
            f"Activate {item.name}: choose working directory",
            [f"{w['alias']}  {w['path']}" for w in worktrees],
        )
        if index is None:
            return
        repo = worktrees[index]["alias"]
    code = lifecycle.open_shell(app.project, item.name, repo, environment)
    app.flash(f"Shell exited ({code}); returned to {item.name}")


def run_ui(opts: argparse.Namespace) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AWMError("Interactive mode requires a TTY")
    os.environ.setdefault("ESCDELAY", "25")
    global_view = not (
        getattr(opts, "root", None) or getattr(opts, "project", None)
    ) and not getattr(opts, "envs", False)
    while True:
        if global_view:
            project_id = curses.wrapper(project_picker)
            if project_id is None:
                return 0
            project = registry.select(project=project_id)
        else:
            project = cli.selected(opts)
        holder = {}

        def launch(scr, project=project, holder=holder):
            app = holder.get("app")
            if app is None:
                app = App(
                    scr, opts, project, "envs" if getattr(opts, "envs", False) else "sandboxes"
                )
            app.scr = scr
            holder["app"] = app
            return app.run()

        while True:
            result = curses.wrapper(launch)
            app = holder["app"]
            if result != "activate":
                break
            try:
                activate_current(app)
            except (AWMError, OSError) as exc:
                app.flash(str(exc), seconds=10)
            app.sb_items, app.env_items = None, None
            app.env_cache.clear()
        if result == "delete":
            items = app.selected_items()
            if app.mode == "envs":
                paths = {item.env.path for item in items}
                names = list(dict.fromkeys(item.sandbox for item in items if item.sandbox))
                if any(item.protected or not item.sandbox for item in items):
                    raise AWMError("Selection contains an unowned or protected environment")
                kwargs = dict(keep_worktrees=True, environment_paths=paths)
                for item in items:
                    print(f"  {item.env.kind} {item.env.path} (worktrees kept)")
            else:
                names = [item.name for item in items]
                kwargs = app.toggles.copy()
                for record in lifecycle.delete(project, names, dry_run=True, **kwargs):
                    cli.print_plan(project, record)
            lifecycle.delete(project, names, dry_run=True, **kwargs)
            if cli.confirmation(f"Delete the selected resources in {project.name}?", False):
                lifecycle.delete(project, names, **kwargs)
                cli.log("Deletion complete")
            if not global_view:
                return 0
        elif not global_view:
            return 0
