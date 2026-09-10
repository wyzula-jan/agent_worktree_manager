# Publishing to PyPI

The distribution is `agent-worktree-manager`; its installed command is `awm`.
The first public release is version `0.2.0`.

## GitHub Trusted Publishing

1. Push the repository, including `.github/workflows/publish.yml`, to GitHub.
2. In your PyPI account, add a [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   for `agent-worktree-manager`. Enter the GitHub owner and repository name,
   workflow filename `publish.yml`, and environment name `pypi`.
3. Create the `pypi` environment in the repository's GitHub Actions settings.
4. Publish a GitHub release with tag `v0.2.0` at the intended commit.

The workflow runs the macOS/Linux CI suite, builds and checks the distributions,
then uploads them to PyPI using a short-lived identity token. No stored PyPI API
secret is needed. The release tag must match `pyproject.toml` and `__init__.py`.
For subsequent releases, increment both versions and publish the matching tag.

## Publish from a local checkout

With PyPI authentication configured locally for Twine:

```sh
python -m pip install -e '.[dev]'
python -m pytest
python -m build
python -m twine check --strict dist/*
python -m twine upload --repository pypi dist/agent_worktree_manager-0.2.0*
```

Before uploading, also run the installed-wheel check described in
[development](development.md). Use Twine's secure prompt or local credential
configuration for the API token; do not commit credentials. A GitHub repository
is not required for this route.

After the first successful upload, remove the pending-publication note from the
README and verify installation from outside the checkout:

```sh
uv tool install --reinstall agent-worktree-manager
awm --version
```
