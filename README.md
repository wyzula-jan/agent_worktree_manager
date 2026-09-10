# Agent Worktree Manager

Working on several branches or with coding agents means keeping track of both
source checkouts and their Python environments. `awm` pairs Git worktrees with
isolated environments and provides a terminal UI to browse and clean them up
across projects.

Start with an environment that already works. AWM reproduces its installed
dependencies and points selected editable packages at new worktrees. Other
editable packages keep their original source links. Install AWM once, separately
from your project environments.

Supports Python 3.10–3.14 on macOS and Linux; Git is required. Standard venv/pip
is the default; uv and conda backends are optional. AWM and your project
environments can use different Python versions.

## Installation

From PyPI, once the first release is published, choose either:

```sh
uv tool install agent-worktree-manager
# Or install into your chosen Python environment:
python -m pip install agent-worktree-manager
```

To install from a source checkout now, run at this repository's root:

```sh
uv tool install --editable .
# Or:
python -m pip install -e .
```

## Register your first project

From your project's Git checkout, register its existing Python environment:

```sh
awm init --name my-project --base /absolute/path/to/base-env --repo app=.
awm doctor
awm create my-task --repo app
awm run my-task -- python --version
awm
```

`--base` is the environment directory, not its Python executable. Use
`--backend uv` for a uv base or `--backend conda` for a conda base. Repeat
`--repo ALIAS=PATH` to include more repositories. Registration writes `awm.toml`
and machine-specific settings in git-ignored `.awm/`, and adds the project to
your global overview. From elsewhere, select it with `awm --project my-project`.

## Use with coding agents

The bundled `awm` skill guides agents through creating, resuming, inspecting,
and cleaning up worktrees with their environments. Install it for either agent:

```sh
# Codex
awm skill --install "$HOME/.agents/skills/awm"
# Claude Code
awm skill --install "$HOME/.claude/skills/awm"
```

In Codex, select it through `/skills` or mention `$awm`. In Claude Code, use
`/awm`. For example: “create a sandbox for fixing the login tests and work there.”
See the [agent guide](docs/agents.md) for custom locations and updating a skill.

## License

[BSD 3-Clause](LICENSE) — Copyright (c) 2026, Jan Wyzula.

## Documentation

[Usage](docs/usage.md) · [Configuration](docs/configuration.md) ·
[AI agent skill](docs/agents.md)
