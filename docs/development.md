# Developer Guide

This document provides guidelines and instructions for setting up a local development environment, running tests, and understanding the project architecture of `gitferret`.

## 1. Local Environment Setup

### Prerequisites
- Python 3.10 or higher
- Git 2.20 or higher

### Virtual Environment & Editable Install
Create and activate a virtual environment, then install the package in editable mode with development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate  # macOS / Linux
# .\.venv\Scripts\activate  # Windows

pip install -e ".[dev]"
```

### Dependency Management
- `requirements.txt`: Runtime dependencies (`textual>=0.86.0`).
- `requirements-dev.txt`: Pinned development dependencies (`pytest`, `ruff`, `textual`).
- `requirements.lock`: Pinned dependency lock reference.


## 2. Running Locally

You can test and run local changes using several methods:

### Direct Module Execution
Run the package module directly from the repository root:

```bash
python3 -m gitferret
```

### Local Runner Script
Use `run.sh`, which automatically detects `.venv` and delegates to Make:

```bash
./run.sh
```

### Makefile Targets
The `Makefile` provides convenient shortcuts:

- `make start`: Installs the package in editable mode and launches `gitferret` for the current working directory.
- `make install-global`: Ensures `.venv` exists, installs the package, and creates symlinks in `~/.local/bin`.
- `make uninstall-global`: Removes the global symlinks from `~/.local/bin`.

## 3. Testing & Code Quality

### Running Tests
Unit tests are written with `pytest` and located in the `tests/` directory:

```bash
pytest tests/
```

### Linting & Formatting
We use `ruff` to verify code quality and enforce import sorting:

```bash
ruff check src/ tests/
```

To automatically fix formatting and import order:

```bash
ruff check --fix src/ tests/
```

## 4. Project Architecture

The codebase follows the standard `src` layout:

```text
gitferret/
├── Makefile                # Automation commands
├── README.md               # User-facing overview and quick start
├── LICENSE                 # MIT License
├── AGENTS.md               # Commit rules and agent guidelines
├── pyproject.toml          # PEP 621 packaging metadata & Hatchling config
├── run.sh                  # Local runner script
├── install.sh              # Global CLI installer
├── docs/                   # Documentation and screenshots
│   ├── development.md      # This developer guide
│   └── tasks.md            # Feature backlog & development notes
├── src/
│   └── gitferret/          # Package source root
│       ├── __init__.py     # Package initialization and exports
│       ├── __main__.py     # Module execution entrypoint (python -m gitferret)
│       └── main.py         # Application logic, Textual TUI, and Git runner
└── tests/
    └── test_gitferret.py   # Unit tests
```

### Packaging Details
- **Build backend**: [Hatchling](https://hatch.pypa.io/latest/) (`hatchling>=1.24.0`)
- **CLI Entrypoints**: Configured under `[project.scripts]` in `pyproject.toml` as `gitferret` and `git-ferret`.

## 5. Commit & Workflow Guidelines

Before committing any changes:
1. Ensure all unit tests pass (`pytest tests/`).
2. Ensure linter passes without warnings (`ruff check src/ tests/`).
3. Follow the **Conventional Commits** format specified in [AGENTS.md](../AGENTS.md) (e.g. `feat: ...`, `fix: ...`, `refactor: ...`, `docs: ...`, `test: ...`).
