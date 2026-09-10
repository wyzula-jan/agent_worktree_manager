# Using awm

## Start with one repository

From your repository, using its existing `.venv` as the base:

```sh
awm init --base .venv
awm doctor
awm create fix-bug --dry-run
awm create fix-bug
awm run fix-bug -- python -m pytest
awm list
awm delete fix-bug --dry-run
awm delete fix-bug
```

If the project is already editable-installed in the base, `awm` finds its install
location and preserves package subdirectories automatically. Otherwise, add the
package's editable target to `awm.toml`, for example `targets = [".[dev]"]`.
Dependencies required by that target must already exist in the installed base.

A sandbox uses the exact Python version of its base. New branches start at the
configured checkout's current `HEAD`; `--ref main` selects a different starting
ref. Existing branch names and destination paths are refused. New sandbox names
are simple slugs; branches use the same name.

## Register and switch between projects

```sh
awm projects list
awm projects add ../another-project
awm --project another-project list
awm list --all --json
awm --root ../another-project doctor
awm projects remove another-project
awm
```

Bare `awm` or `awm ui` opens the global project overview. Select a project with
Enter to inspect its sandboxes; quit that view to return to the overview.
Unavailable locations remain listed. `projects remove` only unregisters a project.

Commands use `--project NAME_OR_ID`, `--root PATH`, or the nearest `awm.toml` above
the current directory. Selectors are mutually exclusive. Duplicate project display
names require IDs. `awm ui --project NAME` opens that project's view directly;
`awm envs` opens the current project's environment view.

The registry lives in the platform's user-data directory (`platformdirs`), with
`AWM_DATA_HOME` as an explicit override. Each project keeps its own git-ignored
state under `.awm`. Repeated sandbox names across projects are independent.

## Multiple repositories and backends

```sh
awm init --root . --name research --base ../envs/research \
  --repo client=client --repo server=server
awm create protocol-fix --repo client --repo server
awm run --repo client protocol-fix -- python -m pytest
```

The `run` command accepts its options **before the sandbox name**; everything after
the name (and optional `--`) is the child command. It forwards the command's exit
status and executes without a shell. It holds a shared project lock for the
foreground command's lifetime, preventing `awm` from deleting its environment.
An environment used outside `awm run` is not automatically locked.

Use `awm init --backend uv --base PATH` or `--backend conda --base PATH` to select a
backend explicitly. uv creates a new venv and installs the captured packages;
conda clones the base with copies and reinstalls pip-managed packages to repair
entry points. No backend is selected simply because its executable is installed.

## Import existing worktrees and environments

Register the project first, then preview adoption using explicit resource paths:

```sh
awm --project research import existing-task \
  --worktree client=/absolute/path/to/client-worktree \
  --env conda=/absolute/path/to/conda-env --dry-run
```

Repeat without `--dry-run`, adding `--yes` to accept the preview. Use `uv=PATH`
or `venv=PATH` for those environment types. Repeat `--worktree` and `--env` for
multiple resources; a project can own imported environments from different
backends regardless of the backend used for creating new sandboxes.

Import preserves existing paths, branches and packages. It records ownership in
`.awm/state.json` so the sandbox appears in the overview; `awm.toml` continues to
describe repository aliases. Importing the same resources again is idempotent.
Worktree-only and environment-only entries are allowed. The `imported` status
means adopted for management, not that dependencies or editable links were
validated. Use the package view to inspect those links before running a task.

Configured base environments belong in project configuration, not in imported
sandbox records. Each project has one base; see [configuration](configuration.md)
for using another base with the same repositories.

## TUI keys

| Key | Action |
| --- | --- |
| Arrows / j / k, PgUp / PgDn, g / G | Navigate |
| Space / a | Select current / all filtered rows |
| / | Filter using all entered words |
| Tab | Switch sandbox/environment views |
| p | Inspect installed packages and editable source paths |
| r | Refresh; reload packages inside the package overlay |
| b / f | Toggle branch deletion / force |
| e / w | Keep environments / keep worktrees |
| Enter / d | Preview selection and confirm deletion |
| q / Esc | Close overlay/view |

Disk sizes load in the background. They describe apparent allocated usage, not
necessarily bytes reclaimed on copy-on-write filesystems. Nearby venvs and, for
conda projects, discoverable conda environments can be inspected. Unowned and
protected environments cannot be selected for deletion; import them explicitly first.

## Safety and reproduction limits

- Ownership is recorded explicitly with directory identities and Git metadata.
  Matching a name never authorizes removal. Import is previewable and opt-in.
- Configured bases, active/running tool environments, conda installation roots,
  and primary checkouts are protected in both CLI and TUI. Force cannot bypass
  ownership checks, changed identities, symlinks, or locked Git worktrees.
- Branches are kept unless `--delete-branch` is requested. Uncommitted changes
  require `--force`. A refused worktree deletion preserves its environment.
- Creation validates the base, records progress, checks package versions,
  dependencies and editable destinations, and records failures. A failed attempt
  rolls back safely removable resources it created; changed worktrees are preserved.
  Inspect failures with `awm list --json`; `awm delete` can clear a rolled-back record.
- Standard venvs are recreated, not copied. Installed metadata is a snapshot, not
  a portable artifact lockfile: required wheels, indexes, native build tools,
  external libraries and local/VCS sources must remain available. This is local,
  same-platform reproduction, not transport of a binary environment to another OS.
- The base must be a dedicated venv or conda environment. System-site-package
  inheritance and legacy `.egg-link` editables are rejected. Reinstall legacy
  editables using a modern PEP 660 build backend before adopting that base.
- Dependency versions are preserved; dependencies are never silently upgraded to
  satisfy changed editable projects. Fix the base deliberately and create a new
  sandbox when dependency requirements change. Updating a base does not update
  existing sandboxes. Keep it stable during creation.
- Package source URLs and versions are stored locally in `.awm/state.json`.
  Keep this directory out of version control. Credential-bearing URL userinfo is
  rejected. Supply index credentials through installer environment variables.

For pip reproduction, installer destination overrides and config files are disabled
so they cannot redirect writes. Index settings such as `PIP_INDEX_URL`,
`PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, and `PIP_NO_INDEX` remain available; use uv's
corresponding environment variables with its backend. Build isolation may need
additional build dependencies even when all runtime dependencies are captured.
