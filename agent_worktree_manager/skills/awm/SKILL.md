---
name: awm
description: Use Agent Worktree Manager (awm) to create, inspect, resume, and clean up isolated Git worktrees with Python environments for coding tasks. Use when the user requests an awm sandbox or an isolated coding workspace with a reproduced base environment.
license: BSD-3-Clause
---

# Agent Worktree Manager

Use the installed `awm` CLI for lifecycle operations. A sandbox pairs local Git
worktrees with a reproduction of an installed Python base; it does not create a
GitHub repository or publish a branch. Keep the user's requested coding task as
the objective, and use the sandbox as its workspace.

## Choose the operation and project

- For a bare invocation or a status request, inspect and summarize; do not create
  or delete resources. Start with `awm projects list --json` and, when useful,
  `awm list --all --json`.
- Check `awm --version` and use `awm --help` or a subcommand's help when needed.
  If unavailable, report the missing CLI and the applicable installation command;
  do not install awm into a managed base or sandbox to make it available.
- Use the user's project selector or the nearest `awm.toml` above the requested
  checkout. Inspect `awm.toml` and `.awm/local.toml` for repository aliases, targets,
  backend, and base. Do not assume the agent's initial directory is the requested
  project. Resolve duplicate project names through IDs from the registry.
- Once resolved, keep an explicit `--project` ID or `--root` path on awm commands,
  including after switching the shell's working directory. They are mutually
  exclusive. Use `awm list --json` for resource paths and status, not naming guesses.
- If initialization is needed, identify the existing base and intended checkout
  roots. Ask only for choices that the task and existing configuration do not
  establish. Use `awm init --base PATH --repo ALIAS=PATH` with a dry run first.
  A shared `awm.toml` only needs `awm init --base PATH` for this machine. Standard
  venv/pip is the default; use `--backend uv` or `--backend conda` when selected.
  Do not manufacture an empty base or alter a base to bypass failed checks.

The commands below use `PROJECT_ID`, `NAME`, and `ALIAS` as parameters: replace
these with the resolved project ID, task name, and configured repository alias.

## Create or resume a coding workspace

1. Run `awm --project PROJECT_ID doctor` and inspect existing sandboxes with
   `awm --project PROJECT_ID list --json`. For a resume request, use the specified
   sandbox and verify its resources before working; do not create a duplicate.
2. For creation, select only repositories needed by the task. If multiple aliases
   are plausible, clarify the selection. Choose a short task slug unless the user
   supplied a name. Preview, then execute the authorized creation:

   ```sh
   awm --project PROJECT_ID create NAME --repo ALIAS --dry-run
   awm --project PROJECT_ID create NAME --repo ALIAS
   awm --project PROJECT_ID list --json
   ```

   Repeat `--repo` for additional selected repositories. The starting ref is the
   configured checkout's current `HEAD`; add `--ref` only for a requested ref.
   Do not resolve branch/path collisions by deleting an existing resource.
3. Read the resulting record and require `ready` after creation. Use its exact
   `worktrees[].path`, `environments[].path`, and branch values. For a resumed
   imported/partial sandbox, verify the chosen environment and worktree exist
   and fit the requested task. Inspect recorded failures before using a sandbox.
4. Direct file edits and Git operations to those worktree paths. A tool's default
   working directory does not change just because a shell command used `cd`.
   Read repository instructions at the selected worktree before making changes.
   Editables marked `shared: true` still point at the original source; leave those
   sources unchanged unless the user separately requested changes there.
5. Run project commands through awm so they use the sandbox interpreter and hold
   its execution lock:

   ```sh
   awm --project PROJECT_ID run --repo ALIAS NAME -- python -m pytest
   ```

   All `run` options precede `NAME`; everything after it is the child command.
   For mixed imported environments, select one with `--env PATH` before `NAME`.
   Use the repository's actual test command. Prefer foreground execution; detached
   processes can outlive the lock. Keep awm's own installation separate from the
   project's Python environment.

On missing artifacts, dependency conflicts, busy locks, or failed creation,
report the actionable error and remaining resources. Do not silently upgrade the
base, edit ownership state, switch backends, or remove resources to force success.

## Inspect and hand off

Use the registry and sandbox JSON for backend, lifecycle status, recorded errors,
editable destinations, and missing resources. Use Git in each returned worktree
for current branch, HEAD, and uncommitted changes. Report project/name, absolute
worktree and environment paths, shared source links, and test results relevant to
the task. Keep a completed coding sandbox available for review.

If GitHub issue or pull-request context is part of the request, use the available
GitHub integration or authenticated `gh` CLI against the actual repository remote.
Distinguish local branches from published PRs. Creating a sandbox alone does not
request a push, PR, merge, or GitHub repository creation.

## Clean up when requested

For the selected sandbox, preview the exact requested deletion flags, inspect
changes, and then execute deletion when already authorized:

```sh
awm --project PROJECT_ID delete NAME --dry-run
awm --project PROJECT_ID delete NAME --yes
```

Branches are kept by default. Add `--delete-branch` only when branch removal was
requested. Dirty worktree refusal preserves its environment; report the changes
and keep them unless the user authorized discarding them. Do not automatically
retry with `--force`, use `rm -rf`, or remove Git worktrees behind awm's ownership
records. Import existing resources only when the user requested adoption, using
explicit paths and an `awm import --dry-run` preview first.
