# Contributing

## Development setup

Use Python 3.10 or later. Install the released shared dependency first, then install this project with its development dependencies:

```bash
python -m pip install https://github.com/CBS-HPC/repokit-common/releases/download/v1.0.0/repokit_common-1.0.0-py3-none-any.whl
python -m pip install -e ".[dev]" --no-deps
python -m pip install requests PyYAML nbformat jsonschema dirhash streamlit
```

## Required checks

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=repokit_dmp --cov-report=term-missing
python -m build
twine check dist/*
```

Release tags must match the version in `pyproject.toml`. The release workflow creates the GitHub release and uploads the wheel, source archive, and `SHA256SUMS` file.
