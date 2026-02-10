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

### <a id="repokit-dmp-dataset"></a>
<details>
<summary><strong>🗃️ <code>repokit-dmp dataset</code></strong></summary>

The `repokit-dmp dataset` command scans your `./data/` folder and registers each dataset into a structured metadata file (`dmp.json`). This helps track the location, structure, and reproducibility of datasets in your project.

It also:
- Removes entries from `dmp.json` if the target file or folder no longer exists.
- Captures metadata such as file size, number of files, formats, and optional provenance info.
- Updates the `README.md` and `DCAS template/dataset_list.md` with dataset tables.

> 📁 This command is automatically run as part of the setup process but can be rerun manually to resync metadata.

#### 🔧 Usage

```bash
repokit-dmp dataset
```

#### ✅ What it does:

- Walks through subfolders in `./data/`
- Registers or updates metadata for each dataset folder or file
- Runs any defined data-generation commands (if present)
- Extracts Git commit hashes for version tracking
- Updates the dataset table in your `README.md`
- Regenerates a DCAS-compatible dataset list (`dataset_list.md`)

> 💡 Dataset metadata is stored in `dmp.json` using a normalized schema.  
> 🔍 All dataset remapping logic happens inside the `repokit.rdm.dataset` module.

---
</details>

### <a id="repokit-dmp-dcas-migration"></a>
<details>
<summary><strong>🚚 <code>repokit-dmp dcas-migration</code></strong></summary>

**Purpose**  
Create a DCAS-ready replication package under `./DCAS template/` by:
- Downloading the Social Science Data Editors’ recommended README template.
- Migrating datasets from your project into the DCAS folder (copying or zipping heavy datasets).
- Mirroring key project artifacts (code, docs, results, locks, `dmp.json`) into the package.
- Updating `dmp.json` (or compatible dataset spec) with a `zip_file` path when a dataset is zipped.

**What it operates on**  
- A dataset specification JSON (default: `./dmp.json`) that contains a top-level array `datasets` with entries like:
  ```json
  {
    "datasets": [
      {
        "data_name": "Example dataset",
        "destination": "./data/02_processed/example_dataset",   // path (relative to project root) to copy/migrate
        "number_of_files": 245                                   // used to decide zip vs. copy
      }
    ]
  }
  ```
  - `destination` → source-relative path of the dataset to migrate (file or folder).  
  - `number_of_files` → if greater than `--zip-threshold`, the folder is zipped and stored in the destination’s parent.

**Default behavior**  
Running the tool with defaults will:
1) Fetch the DCAS README template to `./DCAS template/README_template.md` (if not already present).  
2) For each dataset in `datasets`:
   - If `number_of_files` > `zip-threshold` (default 1000) and source is a **directory**: create `<name>.zip` in the destination’s parent and set `zip_file` in the JSON to the zip’s relative path.
   - Otherwise, copy the file/folder “as is” into `./DCAS template/…`.
3) Copy typical project artifacts into `./DCAS template/`:
   - `README.md`, code folder (based on selected programming language), `docs/`, `results/`, `uv.lock`, `environment.yml`, `requirements.txt`, and `dmp.json`.
4) Update and write back the dataset specification JSON with any new `zip_file` fields.

**CLI usage** (wrapper provided by this template)
```bash
repokit-dmp dcas-migration 
```

**Notes**
- The tool also mirrors key project artifacts to the DCAS package, including your language-specific source tree (Python `./src/`, R `./R/`, Stata `./stata/do/`, MATLAB `./src/`), depending on the project’s configured primary language.
- The README template is pulled from the Social Science Data Editors repository and saved as `README_template.md` so you can incorporate or adapt it when finalizing your DCAS package.

---
</details>

### <a id="repokit-dmp-update"></a>
<details>
<summary><strong>🔄 <code>repokit-dmp update</code></strong></summary><br>

A **headless** command that (re)creates and normalizes your maDMP file **`dmp.json`** in the project root. It pulls sensible defaults from the maDMP schema, your project’s Cookiecutter metadata, and built-in templates, then writes a clean, consistently ordered file.

#### 🧠 What it does
- **Creates** `dmp.json` if missing, or **loads & updates** it if present.
- **Sets/keeps the schema URL** (`dmp.schema`) to the exact GitHub “tree” URL for the detected version (1.0/1.1/1.2).  
  If an existing value matches a known URL, that version is honored; otherwise defaults to **1.2**.
- **Populates core fields** from Cookiecutter (`pyproject.toml` / `cookiecutter.json`) when available:  
  `dmp.title`, `dmp.description`, `dmp.contact` (name, email, ORCID), and `project[0]` title/description.
- **Affiliation inference** from Danish university email domains (CBS, KU, SDU, AU, DTU, AAU, RUC, ITU) with ROR IDs.
- **Adds required fields from the JSON Schema** using schema-aware defaults (no hardcoded key lists).
- **Seeds/normalizes datasets**: ensures `dataset[]` exists and each dataset has at least one `distribution[]`.
- **Sets default license** in `distribution.license[].license_ref` from Cookiecutter `DATA_LICENSE` (e.g., CC-BY-4.0) with today’s `start_date`.
- **Moves custom payloads** under `extension` (e.g., legacy `x_dcas` -> `extension[{ "repokit_info": {...} }]`) and seeds a minimal `repokit_info`.
- **Reorders keys** to a canonical layout (root, dataset, distribution, and common nested objects).
- **Timestamps**: updates `dmp.modified` to current UTC (RFC3339 with trailing `Z`). New files also set `dmp.created`.

#### 🖥️ Usage
```bash
# Installed as a console script:
repokit-dmp update
```

#### 📂 Reads (if present)
- `./dmp.json` (existing DMP to update)
- `pyproject.toml` and/or `cookiecutter.json` (project metadata & `DATA_LICENSE`)

#### 📄 Output
- Writes an ordered, normalized **`./dmp.json`**  
- Prints: `DMP ensured at <abs path>/dmp.json using maDMP <version> schema (ordered).`

---
</details>

### <a id="repokit-dmp-editor"></a>
<details>
<summary><strong>✍️ <code>repokit-dmp editor</code></strong></summary><br>

Interactive **Streamlit** editor for maDMPs with **per-dataset publish** buttons for **Zenodo** and **DeiC Dataverse**.

#### ✨ Features
- **Schema-aware forms** for Root, Projects, and Datasets (same defaults as `repokit-dmp update`).
- In each dataset:
  - `dataset_id` expanded inline for quick edits.
  - Single `distribution` expanded inline (multi-distribution falls back to list UI).
  - **Guardrails**:
    - If `personal_data` or `sensitive_data` is **"yes"**, all `distribution[].data_access` are forced to **"closed"`.
    - If access is **shared/closed**, CC license URLs are removed.
    - If access is **open** and license is empty, **CC-BY-4.0** is added by default.
- **Publish actions**: “Publish to Zenodo” / “Publish to DeiC Dataverse” per dataset.
- **Tokens sidebar**: capture and persist `ZENODO_TOKEN` and `DATAVERSE_TOKEN` into `.env`.
- **Load / Save / Download** with optional schema validation.

#### 🖥️ Usage
```bash
# Default launch (Streamlit app)
repokit-dmp editor

# Headless helper for remote servers (prints SSH port-forward instructions)
repokit-dmp editor ssh
```

#### 🔑 Tokens (for publishing)
- **Zenodo** (Sandbox): set `ZENODO_TOKEN`.
- **DeiC Dataverse**: set `DATAVERSE_TOKEN`.


---
</details>
</details>
