# repokit-dmp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml/badge.svg)](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml)

Data Management Plan (DMP) tooling for research projects. This package can be used **independently** or as part of `repokit`.

## Highlights

- Machine-actionable DMP (maDMP) helpers
- Dataset metadata generation and updates
- Optional integrations for publishing workflows

## Installation

From source:

```bash
git clone https://github.com/CBS-HPC/repokit-dmp.git
cd repokit-dmp
pip install -e .
```

## CLI

```bash
set-dataset --help
dmp-update
dmp-editor
dcas-migration
```

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
