"""Interactive curses UI for awm.

Two modes, switched with Tab at any time (or chosen at launch: ``awm ui`` /
``awm envs``):

* sandboxes — every clone_worktree_env.sh sandbox with its worktrees and envs.
  Check/uncheck and delete through the same plan/confirm pipeline as
  ``awm delete``, so all its safety rules still apply.
* envs — every uv venv under the venv home and every conda env on the machine,
  with python version, disk size, last change and sandbox link. ``p`` lists the
  packages installed in the env (importlib.metadata run with the env's own
  interpreter, so it also works for uv venvs without pip) behind a live
  grep-style filter. Check/uncheck and delete envs.

Base envs, the conda root and the env awm itself runs in are protected and
cannot be selected in envs mode.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_worktree_manager import cli

MODES = ("sandboxes", "envs")


# ---------------------------------------------------------------------------
# Disk sizes: shared cache + background du worker that serves the cursor first
# ---------------------------------------------------------------------------
SIZES: dict[str, int | None] = {}


def _du_kb(path: str | Path) -> int | None:
    r = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return None


class SizeWorker:
    def __init__(self) -> None:
        self._queue: list[str] = []
        self._priority: str | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, paths) -> None:
        with self._lock:
            for p in paths:
                p = str(p)
                if p not in SIZES and p not in self._queue:
                    self._queue.append(p)
            if self._queue and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def prioritize(self, path: str | Path | None) -> None:
        with self._lock:
            self._priority = str(path) if path else None

    def _next(self) -> str | None:
        with self._lock:
            if self._priority in self._queue:
                p = self._priority
                self._queue.remove(p)
                return p
            if self._queue:
                return self._queue.pop(0)
            self._thread = None
            return None

    def _run(self) -> None:
        while (p := self._next()) is not None:
            SIZES[p] = _du_kb(p)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
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


def _matches(text: str, query: str) -> bool:
    """grep-style: every whitespace-separated term must occur (case-insensitive)."""
    t = text.lower()
    return all(term in t for term in query.lower().split())


# ---------------------------------------------------------------------------
# Sandbox metadata
# ---------------------------------------------------------------------------
@dataclass
class WtInfo:
    wt: cli.Worktree
    dirty: int = 0
    last_age: str = "?"
    last_subject: str = ""
    unpushed: str = ""

    @property
    def size_kb(self) -> int | None:
        return SIZES.get(str(self.wt.path))


@dataclass
class Item:
    name: str
    envs: list = field(default_factory=list)  # list[cli.Env]
    wts: list[WtInfo] = field(default_factory=list)
    selected: bool = False

    @property
    def dirty(self) -> bool:
        return any(w.dirty for w in self.wts)

    @property
    def env_size_kb(self) -> int | None:
        sizes = [SIZES.get(e.path) for e in self.envs]
        return sum(filter(None, sizes)) if any(s is not None for s in sizes) else None

    @property
    def total_kb(self) -> int:
        return sum(filter(None, [self.env_size_kb, *(w.size_kb for w in self.wts)]))

    @property
    def sizes_pending(self) -> bool:
        return any(e.path not in SIZES for e in self.envs) or any(
            str(w.wt.path) not in SIZES for w in self.wts if not w.wt.prunable
        )

    @property
    def filter_text(self) -> str:
        return " ".join(
            [self.name, *(e.name for e in self.envs)]
            + [f"{w.wt.repo.name} {w.wt.branch or ''}" for w in self.wts]
        )


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


def gather(
    root: Path, base_env: str, conda_base_env: str, venv_home: Path
) -> tuple[list[Item], str | None]:
    """All sandboxes under *root* with per-worktree metadata, plus the conda exe."""
    items: dict[str, Item] = {}
    for repo in cli.find_repos(root):
        prefix = f"{repo.name}_"
        for wt in cli.worktrees_of(repo):
            if wt.path.name.startswith(prefix) and len(wt.path.name) > len(prefix):
                name = wt.path.name[len(prefix) :]
                items.setdefault(name, Item(name=name)).wts.append(wt_info(wt))

    conda = cli.find_conda()
    sources = (
        (cli.uv_envs(venv_home), "uv", f"{base_env}_"),
        (cli.conda_envs(conda) if conda else {}, "conda", f"{conda_base_env}_"),
    )
    for env_map, kind, env_prefix in sources:
        for env_name, path in env_map.items():
            if env_name.startswith(env_prefix) and len(env_name) > len(env_prefix):
                name = env_name[len(env_prefix) :]
                item = items.setdefault(name, Item(name=name))
                item.envs.append(cli.Env(name=env_name, path=path, kind=kind))

    return [items[k] for k in sorted(items)], conda


# ---------------------------------------------------------------------------
# Environment metadata (all uv venvs + all conda envs)
# ---------------------------------------------------------------------------
@dataclass
class Pkg:
    name: str
    version: str
    editable: str | None  # source dir for editable installs

    @property
    def filter_text(self) -> str:
        return f"{self.name} {self.version} {self.editable or ''}"


@dataclass
class EnvItem:
    env: cli.Env
    python: str = "?"
    sandbox: str | None = None  # sandbox name if this is a <base>_<name> clone
    protected: str | None = None  # reason it cannot be deleted, else None
    changed: float | None = None  # last modification timestamp
    selected: bool = False
    pkgs: list[Pkg] | None = None  # loaded on demand
    pkgs_error: str | None = None
    pkgs_loading: bool = False

    @property
    def name(self) -> str:
        return self.env.name

    @property
    def size_kb(self) -> int | None:
        return SIZES.get(self.env.path)

    @property
    def interpreter(self) -> str:
        return str(Path(self.env.path) / "bin" / "python")

    @property
    def filter_text(self) -> str:
        return f"{self.name} {self.env.kind} {self.python} {self.sandbox or ''}"


def python_version_of(path: str | Path) -> str:
    p = Path(path)
    cfg = p / "pyvenv.cfg"
    if cfg.is_file():
        try:
            for line in cfg.read_text(errors="replace").splitlines():
                key, _, val = line.partition("=")
                if key.strip() in ("version_info", "version") and val.strip():
                    return val.strip()
        except OSError:
            pass
    meta = p / "conda-meta"
    if meta.is_dir():
        for f in meta.glob("python-[0-9]*.json"):
            m = re.match(r"python-(\d+\.\d+(?:\.\d+)?)-", f.name)
            if m:
                return m.group(1)
    return "?" if (p / "bin" / "python").exists() else "-"


def env_changed_ts(path: str | Path) -> float | None:
    p = Path(path)
    candidates = [p, p / "pyvenv.cfg", p / "conda-meta" / "history"]
    candidates += list((p / "lib").glob("python*/site-packages")) if (p / "lib").is_dir() else []
    stamps = []
    for c in candidates:
        try:
            stamps.append(c.stat().st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else None


def _active_prefixes() -> set[str]:
    out = set()
    for p in (sys.prefix, os.environ.get("VIRTUAL_ENV"), os.environ.get("CONDA_PREFIX")):
        if p:
            try:
                out.add(str(Path(p).resolve()))
            except OSError:
                pass
    return out


def make_env_item(
    env: cli.Env, base_env: str, conda_base_env: str, active: set[str] | None = None
) -> EnvItem:
    active = _active_prefixes() if active is None else active
    item = EnvItem(env=env, python=python_version_of(env.path), changed=env_changed_ts(env.path))
    base = base_env if env.kind == "uv" else conda_base_env
    if env.name.startswith(f"{base}_") and len(env.name) > len(base) + 1:
        item.sandbox = env.name[len(base) + 1 :]
    if env.name == base:
        item.protected = "base env"
    elif env.kind == "conda" and Path(env.path).parent.name != "envs":
        item.protected = "conda installation root"
    try:
        if str(Path(env.path).resolve()) in active:
            item.protected = "currently active env"
    except OSError:
        pass
    return item


def gather_envs(
    venv_home: Path, conda: str | None, base_env: str, conda_base_env: str
) -> list[EnvItem]:
    """Every uv venv under *venv_home* and every conda env, uv first."""
    active = _active_prefixes()
    items: list[EnvItem] = []
    for name, path in cli.uv_envs(venv_home).items():
        items.append(make_env_item(cli.Env(name, path, "uv"), base_env, conda_base_env, active))
    if conda:
        for name, path in cli.conda_envs(conda).items():
            if Path(path).parent.name != "envs":
                name = "base"  # the conda installation itself
            items.append(
                make_env_item(cli.Env(name, path, "conda"), base_env, conda_base_env, active)
            )
    items.sort(key=lambda e: (e.env.kind != "uv", e.name.lower()))
    return items


# Runs inside the *target* env's interpreter: works without pip, and reports
# editable installs (the thing worth verifying for worktree-backed sandboxes).
PKG_SCRIPT = r"""
import json, sys
out = []
try:
    import importlib.metadata as md
    for d in md.distributions():
        try:
            name = d.metadata["Name"] or "?"
        except Exception:
            name = "?"
        editable = None
        try:
            raw = d.read_text("direct_url.json")
            if raw:
                du = json.loads(raw)
                if du.get("dir_info", {}).get("editable"):
                    editable = du.get("url", "")
                    if editable.startswith("file://"):
                        editable = editable[7:]
        except Exception:
            pass
        out.append([name, d.version or "?", editable])
except ImportError:
    import pkg_resources
    out = [[d.project_name, d.version, None] for d in pkg_resources.working_set]
print(json.dumps(out))
"""


def list_packages(python: str, timeout: float = 120) -> tuple[list[Pkg], str | None]:
    """(packages, error) for the interpreter at *python*."""
    if not os.access(python, os.X_OK):
        return [], "no python interpreter in this env"
    try:
        r = subprocess.run(
            [python, "-I", "-c", PKG_SCRIPT], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return [], "timed out listing packages"
    except OSError as exc:
        return [], str(exc)
    if r.returncode != 0:
        lines = [line for line in r.stderr.strip().splitlines() if line.strip()]
        return [], lines[-1] if lines else "interpreter failed"
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return [], "unexpected output from interpreter"
    seen: set[str] = set()
    pkgs: list[Pkg] = []
    for name, version, editable in rows:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        pkgs.append(Pkg(name, version, editable))
    pkgs.sort(key=lambda p: p.name.lower())
    return pkgs, None


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------
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
    "sandboxes": "[space] toggle  [a] all  [enter] delete  [p] packages  [/] filter  [tab] envs  [q] quit",
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
    def __init__(
        self,
        stdscr,
        opts: argparse.Namespace,
        root: Path,
        venv_home: Path,
        conda: str | None,
        start_mode: str,
    ) -> None:
        self.scr = stdscr
        self.opts = opts
        self.root = root
        self.venv_home = venv_home
        self.conda = conda
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
            self.sb_items, _ = gather(
                self.root, self.opts.base_env, self.opts.conda_base_env, self.venv_home
            )
            paths = [e.path for i in self.sb_items for e in i.envs]
            paths += [str(w.wt.path) for i in self.sb_items for w in i.wts if not w.wt.prunable]
            self.sizes.add(paths)
        elif mode == "envs" and self.env_items is None:
            self._loading("environments")
            self.env_items = gather_envs(
                self.venv_home, self.conda, self.opts.base_env, self.opts.conda_base_env
            )
            for e in self.env_items:
                self.env_cache[e.env.path] = e
            self.sizes.add([e.env.path for e in self.env_items])

    def env_item_for(self, env: cli.Env) -> EnvItem:
        item = self.env_cache.get(env.path)
        if item is None:
            item = make_env_item(env, self.opts.base_env, self.opts.conda_base_env)
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
        """Returns 'delete' or 'quit' to leave the UI, else None."""
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
        elif ch == ord("/"):
            view.editing = True
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
        return 1 + max(1, len(cur.envs)) + max(1, len(cur.wts))

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
            line2 = f"uv venvs: {self.venv_home}   conda: {self.conda or 'not found'}"
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

        scr.noutrefresh()
        if self.overlay is not None:
            self._draw_overlay()
        curses.doupdate()

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
        return f"{box} {item.name:<{name_w}} {kinds:>8} {env_sz:>6}  {_wt_summary(item)}"

    def _draw_detail(self, y: int, cur, n_sel: int) -> None:
        scr = self.scr
        bold, dim = curses.A_BOLD, curses.A_DIM
        if isinstance(cur, EnvItem):
            total = sum(i.size_kb or 0 for i in self.selected_items())
            _safe_add(scr, y, 1, f"{cur.name}  ({cur.env.kind})", bold)
            _safe_add(
                scr,
                y,
                4 + len(cur.name) + len(cur.env.kind),
                f"    selected: {n_sel}  (~{human_size(total)})",
                dim,
            )
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
        ov = self.overlay
        assert ov is not None
        h, w = self.scr.getmaxyx()
        bh, bw = max(6, h - 2), max(20, w - 4)
        try:
            win = curses.newwin(bh, bw, 1, 2)
        except curses.error:
            return
        win.erase()
        try:
            win.box()
        except curses.error:
            pass
        env = ov.env
        vis = ov.visible
        total = len(env.pkgs or [])
        title = (
            f" packages in {env.name} ({env.env.kind}, python {env.python})  —  {len(vis)}/{total} "
        )
        _safe_add(win, 0, 2, title, curses.A_BOLD)
        cursor = "_" if ov.editing else ""
        _safe_add(win, 1, 2, f"/ {ov.filter}{cursor}", curses.A_BOLD if ov.editing else 0)
        _safe_add(
            win,
            1,
            max(2, bw - 66),
            "[/] filter  [c] clear  [j/k PgUp/PgDn g/G] scroll  [r] reload  [esc] close",
            curses.A_DIM,
        )
        _safe_add(win, 2, 1, "─" * (bw - 2), curses.A_DIM)

        rows = bh - 4
        if env.pkgs_loading:
            _safe_add(win, 3, 2, "loading…  (running the env's interpreter)", curses.A_DIM)
        elif env.pkgs_error:
            _safe_add(win, 3, 2, f"error: {env.pkgs_error}", curses.A_BOLD)
        elif not vis:
            _safe_add(win, 3, 2, "no matches" if ov.filter else "no packages", curses.A_DIM)
        else:
            ov.scroll = max(0, min(ov.scroll, max(0, len(vis) - rows)))
            name_w = min(max(len(p.name) for p in vis) + 2, 40)
            ver_w = min(max(len(p.version) for p in vis) + 2, 24)
            for i, pkg in enumerate(vis[ov.scroll : ov.scroll + rows]):
                line = f"{pkg.name:<{name_w}}{pkg.version:<{ver_w}}"
                if pkg.editable:
                    line += f"-> {pkg.editable}"
                _safe_add(win, 3 + i, 2, line, curses.A_BOLD if pkg.editable else 0)
            if len(vis) > rows:
                pos = f" {ov.scroll + 1}-{min(len(vis), ov.scroll + rows)} of {len(vis)} "
                _safe_add(win, bh - 1, bw - len(pos) - 3, pos, curses.A_DIM)
        win.noutrefresh()

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


# ---------------------------------------------------------------------------
# Deletion of plain envs (envs mode)
# ---------------------------------------------------------------------------
def delete_envs(items: list[EnvItem], conda: str | None, venv_home: Path, root: Path) -> int:
    cli.log("environments to delete:")
    for e in items:
        note = f"   (sandbox '{e.sandbox}': worktrees are kept)" if e.sandbox else ""
        print(f"    {e.env.kind:<5} {e.name:<32} {human_size(e.size_kb):>6}  {e.env.path}{note}")
    try:
        answer = input(f"Delete the {len(items)} env(s) above? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        cli.log("aborted")
        return 1

    errors = 0
    for e in items:
        if e.protected:
            cli.err(f"refusing to delete protected env '{e.name}' ({e.protected})")
            errors += 1
            continue
        if e.env.kind == "conda":
            if conda is None:
                cli.err(f"cannot remove conda env '{e.name}': conda executable not found")
                errors += 1
                continue
            cli.log(f"removing conda env '{e.name}'…")
            r = cli.remove_env_at(conda, e.env.path)
            if r.returncode != 0:
                cli.err(f"conda env remove '{e.name}' failed: {r.stderr.strip()}")
                errors += 1
            else:
                cli.log(f"removed conda env '{e.name}'")
        else:
            cli.log(f"removing uv venv '{e.name}'…")
            try:
                cli.remove_uv_env(e.env.path, venv_home)
            except (ValueError, OSError) as exc:
                cli.err(f"uv venv remove '{e.name}' failed: {exc}")
                errors += 1
            else:
                cli.log(f"removed uv venv '{e.name}'")
    cli.sync_pycharm(root)
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Entry point (wired from cli.cmd_ui / cli.cmd_envs)
# ---------------------------------------------------------------------------
def cmd_ui(opts: argparse.Namespace) -> int:
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        cli.err("interactive mode needs a TTY — use 'awm delete NAME…' / 'awm list' instead")
        return 2

    root = Path(opts.root).expanduser().resolve() if opts.root else cli.default_root()
    if not cli.find_repos(root):
        cli.err(f"no git repos found under {root} (use --root to point at the workspace)")
        return 2
    venv_home = (
        Path(opts.venv_home).expanduser().resolve()
        if opts.venv_home
        else cli.default_venv_home(root)
    )
    conda = cli.find_conda()
    start_mode = "envs" if getattr(opts, "envs", False) else "sandboxes"

    os.environ.setdefault("ESCDELAY", "25")  # make Esc respond immediately
    app_holder: dict[str, App] = {}

    def run(stdscr) -> str:
        app = App(stdscr, opts, root, venv_home, conda, start_mode)
        app_holder["app"] = app
        return app.run()

    result = curses.wrapper(run)
    app = app_holder["app"]
    if result != "delete":
        cli.log("nothing deleted")
        return 0

    if app.mode == "envs":
        return delete_envs(app.selected_items(), conda, venv_home, root)

    t = app.toggles
    ns = argparse.Namespace(
        names=[i.name for i in app.selected_items()],
        root=str(root),
        base_env=opts.base_env,
        conda_base_env=opts.conda_base_env,
        venv_home=str(venv_home),
        dry_run=False,
        force=t["force"],
        yes=False,  # cmd_delete shows the plan and asks for a final y/N
        keep_env=t["keep_env"],
        keep_worktrees=t["keep_worktrees"],
        delete_branch=t["delete_branch"],
    )
    return cli.cmd_delete(ns)
