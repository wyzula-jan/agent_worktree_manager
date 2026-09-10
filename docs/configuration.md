# Project configuration

`awm init` writes a shareable `awm.toml`:

```toml
schema_version = 1
name = "research"

[repositories.client]
path = "client"
targets = [".[dev]"]

[repositories.server]
path = "server"
targets = ["server_lib", "server_api[test]"]
```

Paths resolve relative to the directory containing `awm.toml`. Checkouts may be
nested or located elsewhere; aliases must be unique slugs. Each repository path
must identify a checkout root. Git worktree checkouts are supported as configured
source checkouts. Editable targets are relative to their repository and cannot
escape it. An empty target list infers editable installs already present in the
base. Explicit targets add packages or extras; they do not discard other base
editables. All selected repository packages retain their relative subdirectories.

`.awm/local.toml` holds `schema_version = 1`, a generated `project_id`, and an
`[environment]` table with `backend` (`venv`, `uv`, or `conda`) and `base` (an
existing environment directory). Edit the environment table to change the base
for future sandboxes; run `awm doctor` afterward and `awm projects add .` to update
the registry's cached base protection. The current configuration is always read
when a registered location is available. Registry-cached base paths remain
protected while a project is unavailable.

Each project has one base for new sandboxes. To keep both a venv and a conda base
available for the same repositories, initialize a second configuration directory
with a different project name and point its aliases at the existing checkouts:

```sh
mkdir -p ../project-conda
awm init --root ../project-conda --name project-conda \
  --backend conda --base /absolute/path/to/conda-base \
  --repo app=/absolute/path/to/project
```

Existing sandboxes remain owned by their original project. The new entry manages
its own sandbox state and protects its configured base from deletion.

Machine-specific checkout paths or editable targets can override shared values in
`.awm/local.toml`, using existing aliases:

```toml
[repositories.client]
path = "/home/developer/checkouts/client"
targets = [".[dev]"]
```

Omitted values keep their shared settings. Local overrides cannot introduce new
aliases; add those to `awm.toml` so colleagues can configure the same repositories.

Share `awm.toml`; never copy `.awm` into a different project. A colleague runs
`awm init --base PATH` against the shared configuration to establish their own ID,
base path and state. `init` refuses to overwrite existing local configuration.

New resources live under `.awm/sandboxes/NAME/worktrees/ALIAS` and
`.awm/sandboxes/NAME/env`. `.awm/.gitignore` ignores the entire local directory.
Move project locations using normal Git worktree repair and rebuild environments
whose absolute paths changed; update registration with `awm projects add NEW_PATH`.
`awm` refuses to silently adopt a changed directory identity or rewrite stale paths.

State and registry have independent version fields. Unknown versions and malformed
records fail closed. Registry changes and lifecycle mutations take a per-user lock;
project mutations also take an exclusive project lock. `awm run` takes a shared
project lock. Busy operations fail with a retry message rather than waiting indefinitely.
No daemon or global filesystem scan is required.
