# Development and validation

Use an isolated environment at this repository root:

```sh
python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install -e '.[dev]'
.venv-dev/bin/python -m pytest
.venv-dev/bin/python -m ruff check .
.venv-dev/bin/python -m ruff format --check .
.venv-dev/bin/python -m build
.venv-dev/bin/python -m twine check --strict dist/*
```

Tests create temporary Git repositories, environment directories and a private
registry. Local fixture wheels exercise regular and PEP 660 editable installs
without a package index. The uv integration case runs when uv is installed.

The conda integration case requires `AWM_TEST_CONDA_BASE` pointing to a disposable
or read-only reference conda environment. It clones that reference into its own
temporary base before installing fixture packages, then tests cloning, editable
rebinding, execution and deletion. The reference is not modified. `CONDA_EXE` may
select a conda executable; conda package caches can be isolated with `CONDA_PKGS_DIRS`.

For distribution tests, build the wheel and download its runtime dependency wheels:

```sh
python -m build
python -m pip download --dest .release-check/wheels dist/*.whl
AWM_TEST_WHEELHOUSE="$PWD/.release-check/wheels" python -m pytest tests/test_distribution.py
```

`AWM_TEST_SECOND_PYTHON` can select a second Python version for the two-project
installed-wheel test. It defaults to the test runner's Python if unset. That test
installs the wheel into a fresh tool environment, runs from outside the source
checkout, upgrades/uninstalls the tool, and checks that project resources survive.

CI runs the suite and installed-wheel checks on macOS/Linux and Python 3.10/3.14,
plus native conda tests on both OSes. A CI definition alone is not evidence that
its remote runs passed; consult the actual workflow results for platform validation.
