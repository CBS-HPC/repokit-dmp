# repokit-dmp

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml/badge.svg)](https://github.com/CBS-HPC/repokit-dmp/actions/workflows/ci.yml)

Data Management Plan (DMP) tooling for the Research Template. Works standalone or as part of the repokit toolchain.

## ?? Installation

```bash
pip install repokit-dmp
```

## ?? CLI

| Command | Description |
|---------|-------------|
| `repokit-dmp dataset` | Initialize/update dataset metadata and structure links. |
| `repokit-dmp update` | Create/update `dmp.json` from project metadata. |
| `repokit-dmp editor` | Launch Streamlit editor for DMP + publishing helpers. |
| `repokit-dmp dcas-migration` | Run DCAS migration/validation workflow. |

## ?? Command reference

### <a id="repokit-dmp-dataset"></a>
<details>
<summary><strong>🗃️ <code>repokit-dmp dataset</code></strong></summary>

The `repokit-dmp dataset` command scans your `./data/` folder and registers each dataset into a structured metadata file (`dmp.json`). This helps track the location, structure, and reproducibility of datasets in your project.

It also:

- Removes entries from `dmp.json` if the target file or folder no longer exists
- Captures metadata such as file size, number of files, formats, and optional provenance info
- Updates the `README.md` and `DCAS template/dataset_list.md` with dataset tables

> This command is automatically run as part of the setup process but can be rerun manually to resync metadata.

#### Usage

```bash
repokit-dmp dataset
```

#### What it does

- Walks through subfolders in `./data/`
- Registers or updates metadata for each dataset folder or file
- Runs any defined data-generation commands (if present)
- Extracts Git commit hashes for version tracking
- Updates the dataset table in your `README.md`
- Regenerates a DCAS-compatible dataset list (`dataset_list.md`)

> Dataset metadata is stored in `dmp.json` using a normalized schema.
> All dataset remapping logic happens inside the `repokit.rdm.dataset` module.

---
</details>

### <a id="repokit-dmp-dcas-migration"></a>
<details>
<summary><strong>🚚 <code>repokit-dmp dcas-migration</code></strong></summary>

Purpose

Create a DCAS-ready replication package under `./DCAS template/` by:

- Downloading the Social Science Data Editors’ recommended README template
- Migrating datasets into the DCAS folder (copying or zipping heavy datasets)
- Mirroring key project artifacts (code, docs, results, locks, `dmp.json`) into the package
- Updating `dmp.json` (or compatible dataset spec) with a `zip_file` path when a dataset is zipped

What it operates on

- A dataset specification JSON (default: `./dmp.json`) that contains a top-level array `datasets` with entries like:
  ```json
  {
    "datasets": [
      {
        "data_name": "Example dataset",
        "destination": "./data/02_processed/example_dataset",
        "number_of_files": 245
      }
    ]
  }
  ```
  - `destination` is the source-relative path of the dataset to migrate (file or folder)
  - `number_of_files` decides zip vs. copy when above `--zip-threshold`

Default behavior

Running the tool with defaults will:

1) Fetch the DCAS README template to `./DCAS template/README_template.md` (if not already present)
2) For each dataset in `datasets`:
   - If `number_of_files` > `zip-threshold` (default 1000) and source is a directory: create `<name>.zip` in the destination’s parent and set `zip_file` in the JSON to the zip’s relative path
   - Otherwise, copy the file/folder as is into `./DCAS template/...`
3) Copy typical project artifacts into `./DCAS template/`:
   - `README.md`, code folder (based on selected programming language), `docs/`, `results/`, `uv.lock`, `environment.yml`, `requirements.txt`, and `dmp.json`
4) Update and write back the dataset specification JSON with any new `zip_file` fields

CLI usage

```bash
repokit-dmp dcas-migration
```

Notes

- The tool also mirrors key project artifacts to the DCAS package, including your language-specific source tree (Python `./src/`, R `./R/`, Stata `./stata/do/`, MATLAB `./src/`), depending on the project’s configured primary language
- The README template is pulled from the Social Science Data Editors repository and saved as `README_template.md`

---
</details>

### <a id="repokit-dmp-update"></a>
<details>
<summary><strong>🔄 <code>repokit-dmp update</code></strong></summary>

A headless command that (re)creates and normalizes your maDMP file `dmp.json` in the project root. It pulls defaults from the maDMP schema, your project’s Cookiecutter metadata, and built-in templates, then writes a clean, consistently ordered file.

#### What it does

- Creates `dmp.json` if missing, or loads and updates it if present
- Sets/keeps the schema URL (`dmp.schema`) to the detected version (1.0/1.1/1.2). Defaults to 1.2 if unknown
- Populates core fields from Cookiecutter (`pyproject.toml` / `cookiecutter.json`) when available
- Infers affiliation from Danish university email domains (CBS, KU, SDU, AU, DTU, AAU, RUC, ITU) with ROR IDs
- Adds required fields from the JSON Schema using schema-aware defaults
- Seeds/normalizes datasets (ensures `dataset[]` and at least one `distribution[]`)
- Sets default license in `distribution.license[].license_ref` from Cookiecutter `DATA_LICENSE` with today’s `start_date`
- Moves custom payloads under `extension` and seeds a minimal `repokit_info`
- Reorders keys to a canonical layout
- Updates `dmp.modified` to current UTC (RFC3339 with trailing `Z`)

#### Usage

```bash
repokit-dmp update
```

#### Reads (if present)

- `./dmp.json`
- `pyproject.toml` and/or `cookiecutter.json`

#### Output

- Writes an ordered, normalized `./dmp.json`
- Prints: `DMP ensured at <abs path>/dmp.json using maDMP <version> schema (ordered).`

---
</details>

### <a id="repokit-dmp-editor"></a>
<details>
<summary><strong>✍️ <code>repokit-dmp editor</code></strong></summary>

Interactive Streamlit editor for maDMPs with per-dataset publish buttons for Zenodo and DeiC Dataverse.

#### Features

- Schema-aware forms for Root, Projects, and Datasets (same defaults as `repokit-dmp update`)
- In each dataset:
  - `dataset_id` expanded inline for quick edits
  - Single `distribution` expanded inline (multi-distribution falls back to list UI)
  - Guardrails:
    - If `personal_data` or `sensitive_data` is yes, all `distribution[].data_access` are forced to closed
    - If access is shared/closed, CC license URLs are removed
    - If access is open and license is empty, CC-BY-4.0 is added by default
- Publish actions: “Publish to Zenodo” / “Publish to DeiC Dataverse” per dataset
- Tokens sidebar: capture and persist `ZENODO_TOKEN` and `DATAVERSE_TOKEN` into `.env`
- Load / Save / Download with optional schema validation

#### Usage

```bash
repokit-dmp editor
repokit-dmp editor ssh
```

#### Tokens (for publishing)

- Zenodo (Sandbox): set `ZENODO_TOKEN`
- DeiC Dataverse: set `DATAVERSE_TOKEN`

---
</details>

## ?? License

MIT
