# repokit-dmp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml/badge.svg)](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml)

`repokit-dmp` provides **Data Management Plan (DMP)** tooling for research projects. It is designed to be used standalone or as part of `repokit`.

## What it does

- Create and update machine-actionable DMPs (`dmp.json`)
- Generate dataset entries and metadata
- Support DCAS migration utilities
- Optional editor UI for guided updates

## Requirements

- Python 3.12+

## Install

From PyPI:

```bash
pip install repokit-dmp
```

From wheel (`.whl`):

```bash
# from local dist/
pip install dist/repokit_dmp-0.1-py3-none-any.whl
# or
uv pip install dist/repokit_dmp-0.1-py3-none-any.whl
```

From source:

```bash
git clone https://github.com/CBS-HPC/repokit-dmp.git
cd repokit-dmp
pip install -e .
```

Using uv:

```bash
uv pip install repokit-dmp
```

## Quick start

Run the commands from your **project root** (the tool treats the current working directory as `PROJECT_ROOT`).

Create or update a DMP:

```bash
dmp-update
```

Update dataset metadata:

```bash
set-dataset
```

Open the interactive editor:

```bash
dmp-editor
```

Run DCAS migration helpers:

```bash
dcas-migration
```

## Files and inputs

- `dmp.json` is created/updated in the current working directory.
- If present, `pyproject.toml` and `cookiecutter.json` are used to seed metadata.

## Development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT
