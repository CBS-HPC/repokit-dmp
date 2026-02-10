# repokit-dmp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml/badge.svg)](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml)

Data Management Plan (DMP) tooling for the Research Template. Works standalone or as part of the repokit toolchain.

## Installation

```bash
pip install repokit-dmp
```

## CLI

| Command | Description |
|---------|-------------|
| `repokit-dmp dataset` | Initialize/update dataset metadata and structure links. |
| `repokit-dmp update` | Create/update `dmp.json` from project metadata. |
| `repokit-dmp editor` | Launch Streamlit editor for DMP + publishing helpers. |
| `repokit-dmp dcas-migration` | Run DCAS migration/validation workflow. |

## Quickstart

```bash
repokit-dmp dataset
repokit-dmp update
repokit-dmp editor
```

## License

MIT
