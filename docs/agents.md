# Use awm with a coding agent

The package includes an Agent Skills-compatible `awm` skill. It guides an agent
through creating or resuming a worktree, running tests in its environment,
reporting status, and cleaning up on request. The `awm` CLI must be installed and
available to the agent. PyPI publishing is not needed to use the skill.

## Codex

Install it for all projects:

```sh
awm skill --install "$HOME/.agents/skills/awm"
```

Select it through `/skills`, or mention it explicitly:

```text
$awm create a sandbox for fixing the login tests and work there
$awm show my projects and sandbox status
```

For a Codex installation using `~/.codex/skills` or a custom skill directory,
pass that location instead. Avoid installing duplicate copies in both locations.
If the skill does not appear, start a new session or restart the app.

## Claude Code

```sh
awm skill --install "$HOME/.claude/skills/awm"
```

Invoke it as a slash command:

```text
/awm create a sandbox for fixing the login tests and work there
/awm inspect the login-fix sandbox
/awm delete the login-fix sandbox, keeping its branch
```

In Codex the explicit mention is `$awm`; the literal `/awm` command is Claude
Code's invocation syntax. The same skill content works in both.

## Share or customize

For a project-local skill, install into `.agents/skills/awm` (Codex) or
`.claude/skills/awm` (Claude Code) at that project's root and commit the files.
Other agents supporting `SKILL.md` can use their own discovery directory.

`awm skill` prints the bundled instructions. Repeating installation is harmless
when the bundled files match; different existing contents are preserved. To
update an older or customized copy, compare it with `awm skill`, then merge the
changes or move the old directory before reinstalling. Tool uninstallation
leaves installed skill copies in place.

The skill respects project selection, existing bases, shared editable sources,
and awm's deletion protections. Worktree creation does not publish a GitHub branch
or create a pull request.

See the [skill source](../agent_worktree_manager/skills/awm/SKILL.md),
[Codex skill documentation](https://learn.chatgpt.com/docs/build-skills), and
[Claude Code skill documentation](https://code.claude.com/docs/en/skills).
