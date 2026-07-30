# agent_worktree_manager

Cleanup counterpart to [`clone_worktree_env.sh`](../clone_worktree_env.sh). That script creates a
background-task sandbox out of:

- git worktrees `<repo>_<name>` (branch `<name>`) next to the primary checkouts, and
- a cloned conda env `<base_env>_<name>` (default base: `bec_312`).

`awm` deletes those sandboxes again — both halves, or whichever half still exists — and can do it
for several names in one call.

## Usage

No install needed (stdlib only):

```bash
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

**`awm list [--json]`** — show every sandbox: its worktrees (repo, branch, `*` = uncommitted
changes) and its conda env. Sandboxes that only have an env, or only worktrees, show `-` for the
missing half.

**`awm delete NAME… [flags]`** — remove the sandbox(es). For each name it:

1. finds every worktree registered in any repo under the workspace root whose directory is
   `<repo>_<name>` (slashes in `NAME` map to dashes, mirroring the clone script),
2. `git worktree remove`s them (+ `git worktree prune`), and
3. removes the conda env `<base_env>_<name>` if it exists.

If only the env or only the worktrees are left, the existing half is removed and the missing half
is reported and skipped.

| Flag | Effect |
| --- | --- |
| `-n, --dry-run` | print the plan, delete nothing |
| `-y, --yes` | skip the confirmation prompt (required when non-interactive) |
| `-f, --force` | discard uncommitted changes, delete orphaned worktree dirs, use `-D` for branches |
| `--keep-env` | delete only the worktrees |
| `--keep-worktrees` | delete only the conda env |
| `--delete-branch` | also delete each worktree's branch (`-d`; `-D` with `--force`) |
| `--root PATH` | workspace root (default: `$AWM_ROOT`/`$PSI_ROOT`, else nearest dir containing `clone_worktree_env.sh`) |
| `--base-env NAME` | base env the clones were made from (default: `bec_312`, or `$BASE_ENV`) |

### Safety

- Worktrees with uncommitted changes are refused unless `--force`.
- Branches are **kept** by default, so committed (even unpushed) work survives worktree removal;
  opt in to deletion with `--delete-branch`.
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
conda run -n bec_312 python -m pytest agent_worktree_manager/tests/
```
