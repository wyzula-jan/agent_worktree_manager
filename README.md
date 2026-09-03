# agent_worktree_manager

Cleanup counterpart to [`clone_worktree_env.sh`](../clone_worktree_env.sh). That script creates a background-task
sandbox out of:

- git worktrees `<repo>_<name>` (branch `<name>`) next to the primary checkouts, and
- a uv venv `<root>/.venvs/<base_env>_<name>` (default base: `bec_base`).

`awm` deletes those sandboxes again — both halves, or whichever half still exists — and can do it for several names in
one call.

**Two env flavours.** Since the workspace moved from conda to uv, `awm` finds and deletes both:

| flavour        | location         | default prefix | flag                        |
|----------------|------------------|----------------|-----------------------------|
| uv (current)   | `<root>/.venvs/` | `bec_base_`    | `--base-env`, `--venv-home` |
| conda (legacy) | conda's env dir  | `bec_312_`     | `--conda-base-env`          |

A half-migrated sandbox can have both at once; `delete` removes every env it finds, so the leftover conda envs can be
cleaned up in the same pass. The `ENV` column in `awm list` shows which flavours a sandbox has (`uv`, `conda`, or
`uv, conda`).

uv venvs are deleted with `rm -rf`, but only after two checks: the path must resolve inside
`--venv-home`, and it must actually contain `bin/python`. The primary `bec_base` env has no
`_<name>` suffix, so it is never treated as a sandbox.

## Usage

No install needed (stdlib only):

```bash
./agent_worktree_manager/awm                # interactive sandbox picker (same as `awm ui`)
./agent_worktree_manager/awm envs           # interactive environment browser (Tab switches between the two)
./agent_worktree_manager/awm list
./agent_worktree_manager/awm delete fix-async
./agent_worktree_manager/awm delete fix-async scan-interlock memaudit   # batch
```

Or install it once into any env to get `awm` on the PATH:

```bash
pip install -e ./agent_worktree_manager
```

`python -m agent_worktree_manager …` works too when the repo is on `sys.path`.

### Commands

**`awm ui`** (default when run with no arguments) — full-screen interactive picker. Every sandbox is a row with a
checkbox; a detail pane shows the current row's metadata: every env (flavour, name, path) and their combined disk size,
each worktree's repo, branch, dirty file count, pushed/unpushed state, size, and last commit (age + subject). Sizes are
computed in the background and fill in as they arrive.

Note that on APFS a uv venv's `du` size is mostly copy-on-write clones shared with the uv cache, so the reported size is
an upper bound on what deleting it actually frees.

| Key                       | Action                                                   |
|---------------------------|----------------------------------------------------------|
| `↑`/`↓` or `k`/`j`        | move (`PgUp`/`PgDn`, `g`/`G` jump)                       |
| `space`                   | check/uncheck the current sandbox                        |
| `a`                       | select all / none (of the filtered view)                 |
| `/`                       | filter the list (grep-like: every word must match)       |
| `p`                       | package list of the sandbox's env (see below)            |
| `tab`                     | switch to the environment browser and back               |
| `b`                       | toggle "also delete branches"                            |
| `f`                       | toggle force (discard uncommitted changes)               |
| `e` / `w`                 | toggle keep-envs / keep-worktrees                        |
| `enter` / `d`             | proceed — prints the usual plan and asks for a final y/N |
| `q` / `esc`               | quit without deleting                                    |

Confirmed selections go through exactly the same pipeline as `awm delete`, so all its safety rules (dirty refusal,
branch keeping, orphan-dir proof, base-env protection) still apply.

**`awm envs`** (or `awm ui --envs`, or `tab` from the sandbox picker) — the same interface for *every* environment
on the machine: all uv venvs under the venv home and all conda envs, whether or not they belong to a sandbox. Each
row shows kind, python version, disk size, age of the last change and the sandbox it belongs to (if any); the detail
pane adds the path, the package count once loaded, the sandbox's worktrees, and why an env is protected. The same
keys apply (`space`/`a`/`/`/`enter`/`q`/`tab`), and:

| Key | Action                                                                 |
|-----|------------------------------------------------------------------------|
| `p` | open the **package list** of the current env                           |

The package list runs `importlib.metadata` with the env's *own* interpreter (so it also works for uv venvs, which
have no pip) and shows name, version and — for editable installs — the source directory, which is the quick way to
verify that a sandbox env really points at its worktree. Inside the overlay: `/` types a live filter (grep-like:
`bec widgets` matches rows containing both), `c` clears it, `j`/`k`/`PgUp`/`PgDn`/`g`/`G` scroll, `r` reloads,
`esc`/`q` closes.

Protected envs are shown as `[-]` and cannot be selected: the uv base env (`bec_base`), the conda base env
(`bec_312`), the conda installation root, and whichever env is currently active or running `awm`. Deleting an env
that belongs to a sandbox leaves the sandbox's worktrees in place (the plan says so); use the sandbox picker or
`awm delete` to remove both halves together.

**`awm list [--json]`** — show every sandbox: its worktrees (repo, branch, `*` = uncommitted changes) and its env
flavours. Sandboxes that only have an env, or only worktrees, show `-` for the missing half.

**`awm delete NAME… [flags]`** — remove the sandbox (es). For each name it:

1. finds every worktree registered in any repo under the workspace root whose directory is
   `<repo>_<name>` (slashes in `NAME` map to dashes, mirroring the clone script),
2. `git worktree remove`s them (+ `git worktree prune`), and
3. removes every env it found: the uv venv `.venvs/<base_env>_<name>` and/or the legacy conda env
   `<conda_base_env>_<name>`.

If only the env or only the worktrees are left, the existing half is removed and the missing half is reported and
skipped.

| Flag                    | Effect                                                                                                 |
|-------------------------|--------------------------------------------------------------------------------------------------------|
| `-n, --dry-run`         | print the plan, delete nothing                                                                         |
| `-y, --yes`             | skip the confirmation prompt (required when non-interactive)                                           |
| `-f, --force`           | discard uncommitted changes, delete orphaned worktree dirs, use `-D` for branches                      |
| `--keep-env`            | delete only the worktrees                                                                              |
| `--keep-worktrees`      | delete only the env(s)                                                                                 |
| `--delete-branch`       | also delete each worktree's branch (`-d`; `-D` with `--force`)                                         |
| `--root PATH`           | workspace root (default: `$AWM_ROOT`/`$PSI_ROOT`, else nearest dir containing `clone_worktree_env.sh`) |
| `--base-env NAME`       | primary uv env; sandboxes are `<base-env>_<name>` (default: `bec_base`, or `$BASE_ENV`)                |
| `--conda-base-env NAME` | legacy conda base env still swept for cleanup (default: `bec_312`, or `$CONDA_BASE_ENV`)               |
| `--venv-home PATH`      | directory holding the uv venvs (default: `<root>/.venvs`, or `$VENV_HOME`)                             |

### Safety

- Worktrees with uncommitted changes are refused unless `--force`.
- Branches are **kept** by default, so committed (even unpushed) work survives worktree removal; opt in to deletion with
  `--delete-branch`.
- A directory is only ever `rm -rf`'d when it is provably an orphaned worktree (its `.git` *file*
  points into `<repo>/.git/worktrees/`) **and** `--force` is given. Primary checkouts (`.git`
  directory) and plain data dirs that merely match the naming scheme are never touched.
- Stale registrations (worktree dir already gone by hand) are cleaned with `git worktree prune`.
- The base env itself is never a deletion target.

### Examples

```bash
# see what exists
./agent_worktree_manager/awm list

# preview a batch delete
./agent_worktree_manager/awm delete loadbanner data_api --dry-run

# non-interactive full cleanup, including branches, discarding local changes
./agent_worktree_manager/awm delete old-task -y -f --delete-branch

# the env was already removed by hand — this just removes the worktrees
./agent_worktree_manager/awm delete half-cleaned -y
```

## Tests

```bash
../psi_run.sh bec_base python -m pytest agent_worktree_manager/tests/
```
