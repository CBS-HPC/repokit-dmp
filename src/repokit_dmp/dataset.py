import hashlib
import json
import os
import pathlib
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from typing import Any

from dirhash import dirhash as _dirhash

from repokit_common import (
    JSON_FILENAME,
    PROJECT_ROOT,
    TOML_PATH,
    change_dir,
    check_path_format,
    ensure_correct_kernel,
    read_toml,
    toml_dataset_path,
    write_toml,
)

from . import ensure_project_root
from .dmp import (
    DEFAULT_DMP_PATH,
    LICENSE_LINKS,
    create_or_update_dmp_from_schema,
    data_type_from_path,
    dmp_default_templates,
    ensure_dmp_shape,
    get_repokit_info_payload,
    set_repokit_info_payload,
    load_json,
    norm_rel_urlish,
    now_iso_minute,
    save_json,
    to_bytes_mb,
)

try:
    from repokit.vcs import (
        git_commit,
        git_log_to_file,
        set_datalad,
        datalad_cleaning,
        set_dvc,
        dvc_cleaning,
    )
except Exception:
    git_commit = None
    git_log_to_file = None
    set_datalad = None
    datalad_cleaning = None
    set_dvc = None
    dvc_cleaning = None


DEFAULT_UPDATE_FIELDS = []  # top-level fields


DEFAULT_UPDATE_DIST_FIELDS = ["format", "byte_size"]  # nested fields to update


IGNORE_DICT = {
    # Git
    ".git",
    ".gitignore",
    ".gitkeep",
    ".gitlog",
    ".gitattributes",
    ".gitmodules",  # Git submodules
    # DVC
    ".dvc",
    ".dvcignore",
    "dvc.yaml",
    "dvc.lock",
    ".dvc.tmp",  # DVC temporary files
    # DataLad
    ".datalad",
    ".gitannex",  # Git-annex (used by DataLad)
    # Common metadata/system files
    ".DS_Store",  # macOS
    "Thumbs.db",  # Windows
    "desktop.ini",  # Windows
    ".directory",  # KDE
    # Editor/IDE
    ".vscode",
    ".idea",
    ".vs",
    # Python
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    # Documentation/metadata (optional, depends on your use case)
    "README.md",
    "LICENSE",
}


def get_hash(path, algo: str = "sha256"):
    """
    Get the hash of a file or folder.
    Uses hashlib for files and dirhash for directories.
    For empty directories, returns the digest of empty bytes for stability.
    """
    try:
        if os.path.isfile(path):
            h = hashlib.new(algo)
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()

        elif os.path.isdir(path):
            try:
                return _dirhash(path, algo, ignore=IGNORE_DICT)  # function call, not module
            except Exception:
                # e.g., truly empty directory: give stable hash of empty content
                return hashlib.new(algo, b"").hexdigest()

        else:
            raise ValueError(f"{path} does not exist or is not a valid file or directory.")

    except Exception as e:
        print(f"Error while calculating hash for {path}: {e}")
        return None


def get_file_info(file_paths):
    number_of_files = 0
    total_size = 0.0
    file_formats = set()
    individual_sizes_mb = []
    for path in file_paths:
        number_of_files += 1
        file_size_mb = os.path.getsize(path) / (1024 * 1024)
        total_size += file_size_mb
        individual_sizes_mb.append(int(round(file_size_mb)))
        file_formats.add(os.path.splitext(path)[1].lower())
    return number_of_files, total_size, file_formats, individual_sizes_mb


def get_all_files(destination, ignore=None):
    if ignore is None:
        ignore = {}
    all_files = set()
    for root, dirs, files in os.walk(destination):
        # Filter out ignored directories (modifies in-place to prevent traversal)
        dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]

        # Add non-ignored files
        for file in files:
            if file not in ignore and not file.startswith("."):
                all_files.add(os.path.join(root, file))
    return all_files


def get_data_files(cfg=None, ignore=None, recursive=False):
    if ignore is None:
        ignore = set()
    else:
        ignore = set(ignore)

    if cfg is None:
        cfg, _ = toml_dataset_path()

    parent = pathlib.Path(cfg["parent_path"])
    parent_str = os.fspath(parent)
    use_subdirs = bool(cfg.get("sub_dir", False))

    all_files: list[str] = []
    subdirs: list[str] = []

    try:
        entries = [
            name
            for name in os.listdir(parent_str)
            if name not in ignore and not name.startswith(".")
        ]
    except FileNotFoundError:
        return [], []

    if use_subdirs:
        # Only traverse into subdirectories, not files at parent level
        subdirs = sorted(name for name in entries if os.path.isdir(os.path.join(parent_str, name)))

        for sub in subdirs:
            sub_path = os.path.join(parent_str, sub)
            if recursive:
                iterator = os.walk(sub_path)
            else:
                try:
                    files_here = os.listdir(sub_path)
                except FileNotFoundError:
                    files_here = []
                iterator = [(sub_path, [], files_here)]

            for root, dirs, files in iterator:
                # filter out ignored/hidden dirs
                dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
                for fn in files:
                    if fn not in ignore and not fn.startswith("."):
                        all_files.append(os.path.join(root, fn))
    else:
        for name in entries:
            full = os.path.join(parent_str, name)
            all_files.append(full)
            if os.path.isdir(full):
                subdirs.append(name)
        subdirs.sort()

    return all_files, subdirs


def _is_restricted_dataset_path(destination: str) -> bool:
    """
    Return True when destination is located under a sensitive/proprietary path.
    """
    rel = norm_rel_urlish(destination).replace("\\", "/").lstrip("./")
    parts = [p.lower() for p in pathlib.PurePosixPath(rel).parts]
    return any(p in {"sensitive", "proprietary"} for p in parts)


def _pseudonymize_data_files(data_files: Iterable[str]) -> list[str]:
    """
    Replace full filenames with deterministic pseudonyms while preserving file suffix.
    Example: file_0001.csv, file_0002.parquet
    """
    pseudo_files: list[str] = []
    for idx, path in enumerate(sorted(data_files), start=1):
        suffix = pathlib.Path(path).suffix.lower()
        pseudo_files.append(f"file_{idx:04d}{suffix}")
    return pseudo_files


def _dataset_primary_path(ds: dict) -> str:
    dists = ds.get("distribution") or []
    if isinstance(dists, list):
        for dist in dists:
            if not isinstance(dist, dict):
                continue
            for key in ("access_url", "download_url"):
                val = dist.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    x = get_repokit_info_payload(ds) or {}
    dest = x.get("destination")
    return dest.strip() if isinstance(dest, str) else ""


def _to_project_relative(path_value: str) -> str | None:
    raw = (path_value or "").strip()
    if not raw:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return None
    p = pathlib.Path(raw)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except Exception:
            return None
    else:
        rel = p.as_posix()
    rel = rel.lstrip("./").strip()
    return rel or None


def _gitignore_entry(rel_path: str) -> str:
    p = PROJECT_ROOT / rel_path
    if p.exists() and p.is_dir():
        return f"/{rel_path.rstrip('/')}/"
    return f"/{rel_path.rstrip('/')}"


def _upsert_gitignore_patterns(gitignore_path: pathlib.Path, entries: list[str]) -> bool:
    if not entries:
        return False
    existing_lines: list[str] = []
    if gitignore_path.exists():
        try:
            existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            existing_lines = []
    existing_set = {ln.strip() for ln in existing_lines if ln.strip()}
    to_add = [e for e in entries if e.strip() and e.strip() not in existing_set]
    if not to_add:
        return False
    out = list(existing_lines)
    if out and out[-1].strip() != "":
        out.append("")
    out.extend(to_add)
    gitignore_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return True


def _upsert_agent_ignore_patterns(entries: list[str]) -> bool:
    changed = False
    for name in (
        ".codexignore",
        ".claudeignore",
        ".cursorignore",
        ".opencodeignore",
        ".copilotignore",
    ):
        p = PROJECT_ROOT / name
        if p.exists():
            changed |= _upsert_gitignore_patterns(p, entries)
    return changed


def _regenerate_data_gitlog_if_present() -> bool:
    data_root = (PROJECT_ROOT / "data").resolve()
    if not (data_root / ".git").exists():
        return False
    gitlog = data_root / ".gitlog"
    if not gitlog.exists():
        return False

    try:
        with gitlog.open("w", encoding="utf-8", newline="\n") as fh:
            proc = subprocess.run(
                ["git", "log", "--all", "--pretty=fuller", "--stat"],
                cwd=str(data_root),
                stdout=fh,
                stderr=subprocess.DEVNULL,
                check=False,
                text=True,
            )
        return proc.returncode == 0
    except Exception:
        return False


def _sync_sensitive_policy_artifacts_from_dmp(json_path: str) -> bool:
    data = load_json(json_path)
    dmp = ensure_dmp_shape(data).get("dmp", {})
    datasets = dmp.get("dataset", []) or []

    sensitive_rel_paths: list[str] = []
    seen: set[str] = set()
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        if str(ds.get("sensitive_data", "")).strip().lower() != "yes":
            continue
        rel = _to_project_relative(_dataset_primary_path(ds))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        sensitive_rel_paths.append(rel)

    changed = False

    if sensitive_rel_paths:
        root_entries = [_gitignore_entry(p) for p in sensitive_rel_paths]
        changed |= _upsert_gitignore_patterns(PROJECT_ROOT / ".gitignore", root_entries)
        changed |= _upsert_agent_ignore_patterns(root_entries)

        data_root = (PROJECT_ROOT / "data").resolve()
        if (data_root / ".git").exists():
            data_entries: list[str] = []
            for rel in sensitive_rel_paths:
                full = (PROJECT_ROOT / rel).resolve()
                try:
                    data_rel = full.relative_to(data_root).as_posix()
                except Exception:
                    continue
                if not data_rel:
                    continue
                if full.exists() and full.is_dir():
                    data_entries.append(f"/{data_rel.rstrip('/')}/")
                else:
                    data_entries.append(f"/{data_rel.rstrip('/')}")
            changed |= _upsert_gitignore_patterns(data_root / ".gitignore", data_entries)

        cfg = (
            read_toml(
                folder=str(PROJECT_ROOT),
                json_filename=JSON_FILENAME,
                tool_name="data_policy",
                toml_path=TOML_PATH,
            )
            or {}
        )
        existing = cfg.get("patterns", [])
        if isinstance(existing, str):
            existing_list = [existing]
        elif isinstance(existing, list):
            existing_list = [p for p in existing if isinstance(p, str) and p.strip()]
        else:
            existing_list = []
        merged = list(existing_list)
        existing_set = {p.strip() for p in existing_list if p.strip()}
        for rel in sensitive_rel_paths:
            if rel not in existing_set:
                merged.append(rel)
                existing_set.add(rel)
        if merged != existing_list:
            write_toml(
                data={"patterns": merged},
                folder=str(PROJECT_ROOT),
                json_filename=JSON_FILENAME,
                tool_name="data_policy",
                toml_path=TOML_PATH,
            )
            changed = True

    changed |= _regenerate_data_gitlog_if_present()
    return changed


def datasets_to_json(
    json_path=DEFAULT_DMP_PATH,
    entry=None,
    update_fields: list[str] | None = None,
    update_distribution_fields: list[str] | None = None,
    bump_modified_on_distribution_updates: bool = False,
    do_print: bool = True,
):
    """
    Upsert a dataset entry into {"dmp": {"dataset": [...]}}.

    Matching:
      - prefer by distribution rel URL (url_acess/url_access/access_url/download_url)
      - else by title

    Update policy (when a match is found):
      - changed_flag := (existing.repokit_info.hash != entry.repokit_info.hash)
      - overwrite ONLY:
          * repokit_info payload in `extension` (if provided)
          * top-level fields in `update_fields`
          * nested distribution fields in `update_distribution_fields`, matched by rel URL
      - modified := now_iso_minute() iff changed_flag
        (or also when distribution fields changed, if bump_modified_on_distribution_updates=True)
    """
    if update_fields is None:
        update_fields = DEFAULT_UPDATE_FIELDS
    if update_distribution_fields is None:
        update_distribution_fields = DEFAULT_UPDATE_DIST_FIELDS

    json_path = PROJECT_ROOT / pathlib.Path(json_path)
    data = load_json(json_path)
    dmp = data["dmp"]
    datasets = dmp.get("dataset", [])

    any_change_flag = False

    def _extract_rel_url(dist: dict) -> str | None:
        for k in ("url_acess", "url_access", "access_url", "download_url"):
            v = (dist or {}).get(k)
            if v:
                return norm_rel_urlish(v)
        return None

    def _collect_rel_urls(ds: dict) -> set[str]:
        out: set[str] = set()
        for d in (ds or {}).get("distribution", []) or []:
            u = _extract_rel_url(d)
            if u:
                out.add(u)
        return out

    new_rel_urls = _collect_rel_urls(entry or {})
    idx = None
    if new_rel_urls:
        for i, ds in enumerate(datasets):
            if _collect_rel_urls(ds) & new_rel_urls:
                idx = i
                break
    else:
        for i, ds in enumerate(datasets):
            if (ds or {}).get("title") == (entry or {}).get("title"):
                idx = i
                break

    if idx is not None:
        existing = datasets[idx]
        merged = deepcopy(existing)

        # --- 1) changed_flag from repokit_info.hash only ---
        existing_x = get_repokit_info_payload(existing) or {}
        entry_x = get_repokit_info_payload(entry or {}) or {}
        existing_hash = existing_x.get("hash")
        entry_hash = entry_x.get("hash")
        changed_flag = existing_hash != entry_hash

        # --- 2) optionally replace/insert repokit_info payload ---
        if entry_x:
            set_repokit_info_payload(merged, entry_x)

        # --- 3) overwrite ONLY whitelisted top-level fields ---
        for k in update_fields:
            if k in (entry or {}) and (entry[k] is not None):
                merged[k] = entry[k]

        # --- 4) overwrite ONLY whitelisted nested fields in distribution (URL-matched) ---
        dist_changed = False
        if "distribution" in (entry or {}):
            incoming_list = entry.get("distribution") or []
            existing_list = list(merged.get("distribution") or [])
            # map existing distributions by rel URL
            existing_by_u = {}
            for d in existing_list:
                u = _extract_rel_url(d or {})
                if u:
                    existing_by_u[u] = d
            # apply field updates for matched items
            for s in incoming_list:
                u = _extract_rel_url(s or {})
                if not u or u not in existing_by_u:
                    continue
                tgt = existing_by_u[u]
                for f in update_distribution_fields:
                    if f in s and s[f] is not None and s[f] != tgt.get(f):
                        tgt[f] = s[f]
                        dist_changed = True
            if dist_changed:
                merged["distribution"] = existing_list

        # --- 5) modified policy ---
        if changed_flag or (bump_modified_on_distribution_updates and dist_changed):
            merged["modified"] = now_iso_minute()
        else:
            merged["modified"] = existing.get("modified")

        record_changed = merged != existing
        datasets[idx] = merged
        if record_changed:
            any_change_flag = True

        if changed_flag:
            if do_print:
                print(f"Updated DMP entry for {existing_x.get('title', merged.get('title'))}.")
            any_change_flag = True
        else:
            if do_print:
                print(
                    f"No changes detected for DMP entry: {existing_x.get('title', merged.get('title'))}."
                )

    else:
        entry_x = get_repokit_info_payload(entry or {}) or {}
        datasets.append(entry)
        if do_print:
            print(f"Added DMP entry for {entry_x.get('destination', entry.get('title'))}.")
        any_change_flag = True

    # Sort by repokit_info.data_type then title
    def _sort_key(ds):
        x = get_repokit_info_payload(ds) or {}
        return (x.get("data_type") or "", ds.get("title") or "")

    datasets.sort(key=_sort_key)

    dmp["dataset"] = datasets
    if any_change_flag:
        dmp["modified"] = now_iso_minute()
        save_json(json_path, data)

    return any_change_flag, json_path


def remove_missing_datasets(json_path: str | os.PathLike = DEFAULT_DMP_PATH):
    """
    Ensure the DMP file exists and is shaped. For any dataset whose
    access/download URL (or extension.repokit_info.destination) no longer exists on disk,
    DO NOT delete the dataset; instead set:
      - extension.repokit_info = {}  (as {"repokit_info": {}} in a list-shaped extension)
      - distribution[*].access_url = ""

    Returns the absolute Path to the JSON file.
    """
    # Resolve path relative to project root (3 levels up from this file)
    root = PROJECT_ROOT
    json_path = (root / pathlib.Path(json_path)).resolve()

    # ---- Load & shape ----
    data = load_json(json_path) or {}
    if not isinstance(data, dict):
        data = {}

    dmp = data.get("dmp") or {}
    data["dmp"] = dmp  # reattach so mutations persist
    datasets = dmp.get("dataset")
    if not isinstance(datasets, list):
        datasets = []
    dmp["dataset"] = datasets  # ensure list exists

    # ---- Helpers ----
    def _looks_remote(value: str | None) -> bool:
        if not value or not isinstance(value, str):
            return False
        v = value.strip()
        if not v:
            return False
        if "://" in v:
            return True
        if v.startswith("www."):
            return True
        # Treat rclone-like refs (e.g., dropbox:path) as remote, but not Windows drives (C:\...)
        if ":" in v and not (len(v) >= 2 and v[1] == ":"):
            return True
        return False

    def _exists_on_disk(ds: dict) -> bool:
        # any distribution URL that resolves on local FS OR looks like a remote URL/ref
        for dist in ds.get("distribution") or []:
            if not isinstance(dist, dict):
                continue
            p = dist.get("access_url") or dist.get("download_url")
            if not p:
                continue
            if _looks_remote(p):
                return True
            if os.path.exists(p) or os.path.exists(str(p).replace("/", os.sep)):
                return True

        # extension.repokit_info.destination
        x = get_repokit_info_payload(ds) or {}
        dest = x.get("destination")
        if _looks_remote(dest):
            return True
        return bool(dest and os.path.exists(dest))

    def _set_repokit_info_to_empty(ds: dict) -> None:
        """
        Ensure extension has exactly ONE repokit_info entry formed as {"repokit_info": {}}.
        - If extension is a dict: extension["repokit_info"] = {}
        - If extension is a list: remove any repokit_info variants and append {"repokit_info": {}}
          Variants removed: {"repokit_info": ...}, {"name":"repokit_info",...}, {"extension":"repokit_info",...}
        """
        exts = ds.get("extension")

        # Dict-shaped extension
        if isinstance(exts, dict):
            exts["repokit_info"] = {}
            return

        # List-shaped (or missing): normalize to list and enforce single {"repokit_info": {}}
        if not isinstance(exts, list):
            ds["extension"] = [{"repokit_info": {}}]
            return

        new_exts = []
        for ext in exts:
            if not isinstance(ext, dict):
                new_exts.append(ext)
                continue
            # Drop any prior repokit_info in any known shape
            if "repokit_info" in ext or ext.get("name") == "repokit_info" or ext.get("extension") == "repokit_info":
                continue
            new_exts.append(ext)

        new_exts.append({"repokit_info": {}})
        ds["extension"] = new_exts

    # ---- Main pass ----
    updated = 0
    for ds in datasets:
        if not _exists_on_disk(ds):
            _set_repokit_info_to_empty(ds)
            # Clear access_url in all distributions
            for dist in ds.get("distribution") or []:
                if isinstance(dist, dict):
                    dist["access_url"] = ""
            updated += 1

    if updated:
        dmp["modified"] = now_iso_minute()
        print(f"Updated {updated} dataset(s).")
        save_json(json_path, data)

    return json_path


def dataset(destination, json_path=DEFAULT_DMP_PATH, do_print: bool = True):
    def make_dataset_entry(name, distribution, repokit_info_payload):
        templates = dmp_default_templates()

        # Start from a deep copy so the global template isn't mutated
        entry = deepcopy(templates["dataset"])

        # Simple overlays
        entry["title"] = name
        entry["issued"] = now_iso_minute()
        entry["modified"] = now_iso_minute()

        # distribution must be a list with one object; merge with its template
        dist = deepcopy(templates["distribution"])
        if isinstance(distribution, dict):
            dist.update(distribution)  # shallow overlay
        entry["distribution"] = [dist]

        # repokit_info lives under dataset.extension as a single item: {"repokit_info": {...}}
        xdcas = deepcopy(templates["repokit_info"])
        if isinstance(repokit_info_payload, dict):
            xdcas.update(repokit_info_payload)
        entry["extension"] = [{"repokit_info": xdcas}]

        return entry

    def make_repokit_info_payload(
        *,
        data_type: str | None = None,
        destination: str | None = None,
        number_of_files: int | None = None,
        total_size_mb: float | None = None,
        file_formats: Iterable[str] | None = None,
        data_files: Iterable[str] | None = None,
        data_size_mb: Iterable[float] | None = None,
        hash_value: str | None = None,  # 'hash' is a builtin, so use hash_value
    ) -> dict[str, Any]:
        """
        Build repokit_info by loading templates['repokit_info'] and only updating fields
        that already exist in the template.
        """
        templates = dmp_default_templates()
        x = deepcopy(templates["repokit_info"])

        # Prepare candidate updates (normalize types lightly)
        updates: dict[str, Any] = {
            "data_type": data_type,
            "destination": norm_rel_urlish(destination) if destination is not None else None,
            "number_of_files": int(number_of_files) if number_of_files is not None else None,
            "total_size_mb": int(round(total_size_mb)) if total_size_mb is not None else None,
            "file_formats": list(file_formats) if file_formats is not None else None,
            "data_files": list(data_files) if data_files is not None else None,
            "data_size_mb": list(data_size_mb) if data_size_mb is not None else None,
            "hash": hash_value,
        }

        # Only update keys that already exist in the template AND have a non-None value
        for k, v in updates.items():
            if v is not None and k in x:
                x[k] = v

        return x

    cookie = (
        read_toml(
            folder=str(PROJECT_ROOT),
            json_filename="cookiecutter.json",
            tool_name="cookiecutter",
            toml_path="pyproject.toml",
        )
        or {}
    )

    destination = check_path_format(destination)

    if os.path.isfile(destination):
        data_files = [destination]
    else:
        os.makedirs(destination, exist_ok=True)
        data_files = sorted(get_all_files(destination, ignore=IGNORE_DICT))

    number_of_files, total_size_mb, file_formats, individual_sizes_mb = get_file_info(data_files)

    name = os.path.basename(destination)
    data_type = data_type_from_path(destination)

    is_restricted_path = _is_restricted_dataset_path(destination)
    data_access = "closed" if is_restricted_path else ("open" if data_files else "closed")
    license_ref = "" if is_restricted_path else LICENSE_LINKS.get(cookie.get("DATA_LICENSE"), "")
    repokit_data_files = (
        _pseudonymize_data_files(data_files) if is_restricted_path else list(data_files)
    )

    # distribution (complete RDA-DMP shape with defaults; 1.2-compliant)
    distribution = {
        # "title": name,
        "access_url": norm_rel_urlish(destination),
        # "download_url": "",
        "format": [ext.strip(".") for ext in sorted(file_formats)],
        "byte_size": to_bytes_mb(total_size_mb),
        "data_access": data_access,
        # "host": {"title": "Project repository", "url": ""},
        "available_until": "",
        # "description": "",
        "license": [
            {
                "license_ref": license_ref,
                "start_date": datetime.now().strftime("%Y-%m-%d"),
            }
        ],
    }

    # DCAS payload wrapped under dataset.extension
    repokit_info_payload = make_repokit_info_payload(
        data_type=data_type,
        destination=destination,
        number_of_files=number_of_files,
        total_size_mb=int(round(total_size_mb)),
        file_formats=sorted(list(file_formats)),
        data_files=repokit_data_files,
        data_size_mb=individual_sizes_mb,
        hash_value=get_hash(destination),
    )

    entry = make_dataset_entry(name, distribution, repokit_info_payload)
    if is_restricted_path:
        entry["sensitive_data"] = "yes"

    update_fields = list(DEFAULT_UPDATE_FIELDS)
    dist_update_fields = list(DEFAULT_UPDATE_DIST_FIELDS)
    if is_restricted_path:
        update_fields.append("sensitive_data")
        dist_update_fields.extend(["data_access", "license"])

    change_flag, json_path = datasets_to_json(
        json_path=json_path,
        entry=entry,
        update_fields=update_fields,
        update_distribution_fields=dist_update_fields,
        do_print=do_print,
    )

    return change_flag, json_path


def generate_dataset_table(
    json_path: str,
    file_descriptions: dict[str, str] | None = None,
    include_hash: bool = False,
) -> tuple[str | None, str | None]:
    if not os.path.exists(json_path):
        return None, None

    with open(json_path, encoding="utf-8") as fh:
        json_data = json.load(fh)

    dmp = ensure_dmp_shape(json_data)["dmp"]
    datasets = dmp.get("dataset", [])

    hidden_fields = set()
    if not include_hash:
        hidden_fields.add("hash")

    def is_nonempty(val):
        return val not in (None, "", [], {}, "N/A", "Not provided")

    def safe_str(val):
        return "N/A" if val in (None, "", [], {}, "Not provided") else str(val)

    rows = []
    dynamic_id_fields: set[str] = set()  # <-- collect dynamic keys from dataset_id.type

    for ds in datasets:
        x = get_repokit_info_payload(ds) or {}
        dist = (ds.get("distribution") or [{}])[0]
        destination = dist.get("download_url") or dist.get("access_url") or x.get("destination")
        is_restricted = _is_restricted_dataset_path(destination or "")

        raw_access = (dist.get("data_access") or "").strip().lower()
        data_access = "closed" if is_restricted else (raw_access or "closed")

        licenses = dist.get("license") or []
        if isinstance(licenses, dict):
            licenses = [licenses]
        if licenses and isinstance(licenses[0], dict):
            license_ref = (licenses[0].get("license_ref") or "").strip()
        else:
            license_ref = ""
        if is_restricted:
            license_ref = ""

        # Build the row (unchanged fields kept as-is)
        # Normalize file formats to a list for safe rendering
        fmts = dist.get("format")
        if isinstance(fmts, str):
            fmts_list = [fmts]
        elif isinstance(fmts, (list, set, tuple)):
            fmts_list = list(fmts)
        else:
            fmts_list = []
        fmts_list = sorted(list(set(fmts_list)))

        row = {
            "data_name": ds.get("title"),
            "destination": destination,
            "data_access": data_access,
            "license_ref": license_ref,
            "created": ds.get("issued"),
            "lastest_change": ds.get("modified"),
            "hash": x.get("hash"),
            "provided": "Provided" if x.get("data_files") else "Can be re-created",
            "number_of_files": x.get("number_of_files"),
            "total_size_mb": x.get("total_size_mb")
            if x.get("total_size_mb") is not None
            else int(round((dist.get("byte_size") or 0) / (1024 * 1024))),
            "file_formats": fmts_list,
            "zip_file": None,
            "description": ds.get("description"),
            "_dtype": x.get("data_type") or data_type_from_path(x.get("destination") or ""),
            "_files": x.get("data_files") or [],
            "_sizes": x.get("data_size_mb") or [],
        }

        # --- Dynamic identifier field ---
        ds_id = ds.get("dataset_id") or {}
        if isinstance(ds_id, list) and ds_id:
            ds_id = ds_id[0]

        # Coerce empty/whitespace strings to None
        raw_identifier = ds_id.get("identifier")
        if isinstance(raw_identifier, str):
            ds_id_identifier = raw_identifier.strip() or None
        else:
            ds_id_identifier = raw_identifier

        raw_type = ds_id.get("type")
        ds_id_type = (raw_type.strip() if isinstance(raw_type, str) else raw_type) or None

        # Add a dynamic field named by the identifier type, with value = identifier (possibly None)
        if ds_id_type:
            row[str(ds_id_type)] = ds_id_identifier
            dynamic_id_fields.add(str(ds_id_type))

        rows.append(row)

    grouped = defaultdict(list)
    for r in rows:
        grouped[r["_dtype"]].append(r)

    # Standard (fixed) columns
    standard_fields = {
        "data_name": "Name",
        "destination": "Location",
        "data_access": "Access",
        "license_ref": "License Ref",
        "created": "Created",
        "lastest_change": "Lastest Change",
        "hash": "Hash",
        "provided": "Provided",
        "number_of_files": "Number of Files",
        "total_size_mb": "Total Size (MB)",
        "file_formats": "File Formats",
        "zip_file": "Zip File",
        "description": "Description",
    }
    if not include_hash:
        standard_fields.pop("hash", None)

    # Add dynamic identifier columns (header = key itself)
    for dyn in sorted(dynamic_id_fields):
        standard_fields[dyn] = dyn

    # Pick the active columns to show (non-empty in at least one row)
    active_fields = [
        k
        for k in standard_fields
        if k not in hidden_fields and any(is_nonempty(r.get(k)) for r in rows)
    ]

    summary_header = "| " + " | ".join([standard_fields[k] for k in active_fields]) + " |\n"
    summary_divider = (
        "| " + " | ".join(["-" * len(standard_fields[k]) for k in active_fields]) + " |\n"
    )

    base_detail = [
        "data_name",
        "data_files",
        "destination",
        "data_access",
        "license_ref",
        "created",
        "lastest_change",
        "provided",
        "data_size",
    ]
    if (
        include_hash
        and "hash" not in hidden_fields
        and any(is_nonempty(r.get("hash")) for r in rows)
    ):
        base_detail.insert(5, "hash")

    # Include dynamic identifier fields in the detail table as well
    base_detail = base_detail + sorted(dynamic_id_fields)

    detail_header = "| " + " | ".join([f.replace("_", " ").title() for f in base_detail]) + " |\n"
    detail_divider = "| " + " | ".join(["-" * len(f) for f in base_detail]) + " |\n"

    summary_blocks: list[str] = []
    detail_blocks: list[str] = []

    for dtype, entries in sorted(grouped.items()):
        desc = (
            f" <- {file_descriptions.get(dtype, '')}"
            if (file_descriptions and dtype in file_descriptions)
            else None
        )
        header = f"### {dtype} {desc}\n" if desc else f"### {dtype}\n"
        summary_blocks.append(header + summary_header + summary_divider)

        need_detail = any(len(r["_files"]) > 1 for r in entries)
        if need_detail:
            detail_blocks.append(header + detail_header + detail_divider)

        for r in entries:
            # Summary row
            vals = []
            for k in active_fields:
                if k == "file_formats":
                    fmts = r.get(k) or []
                    if isinstance(fmts, (list, tuple, set)):
                        val = "; ".join("." + f for f in fmts) or "N/A"
                    else:
                        val = "." + str(fmts) if fmts else "N/A"
                else:
                    val = r.get(k, "N/A")
                vals.append(safe_str(val))
            summary_blocks.append("| " + " | ".join(vals) + " |\n")

            # Detail rows
            if need_detail:
                files = r["_files"]
                sizes = r["_sizes"]
                if len(sizes) < len(files):
                    sizes += ["?"] * (len(files) - len(sizes))
                for f, sz in zip(files, sizes, strict=False):
                    detail_vals = []
                    for k in base_detail:
                        if k == "data_files":
                            detail_vals.append(safe_str(f))
                        elif k == "data_size":
                            detail_vals.append(safe_str(sz))
                        else:
                            detail_vals.append(safe_str(r.get(k)))
                    detail_blocks.append("| " + " | ".join(detail_vals) + " |\n")

        summary_blocks.append("\n")
        if need_detail:
            detail_blocks.append("\n")

    return "".join(summary_blocks), "".join(detail_blocks)


def dataset_to_readme(markdown_table: str, readme_file: str = "./README.md", do_print: bool = True):
    section_title = "**The following datasets are included in the project:**"
    readme_path = PROJECT_ROOT / pathlib.Path(readme_file)
    new_section = f"{section_title}\n\n{markdown_table.strip()}\n</details>"
    try:
        content = readme_path.read_text(encoding="utf-8")
        if section_title in content:
            start = content.find(section_title)
            closing_tag = "</details>"
            close_idx = content.find(closing_tag, start)
            if close_idx != -1:
                end = close_idx + len(closing_tag)
            else:
                end = content.find("\n## ", start + len(section_title))
                end = end if end != -1 else len(content)
            updated = content[:start] + new_section + content[end:]
        else:
            updated = content.rstrip() + "\n\n" + new_section
    except FileNotFoundError:
        updated = new_section

    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(updated.strip(), encoding="utf-8")
    if do_print:
        print(f"{readme_path} successfully updated with dataset section.")


def dataset_path_update(
    data_files: list[str] | None = None, dmp_path: str = DEFAULT_DMP_PATH, git_msg: str = None
):
    if isinstance(data_files, str):
        data_files = [data_files]

    if not data_files:
        return
    if not git_msg:
        git_msg = f"Setting dataset path for: {data_files[0]}"

    os.chdir(PROJECT_ROOT)

    DEFAULT_DATASET_PATH, _ = toml_dataset_path()

    file_descriptions = read_toml(
        folder=PROJECT_ROOT,
        json_filename="./file_descriptions.json",
        tool_name="file_descriptions",
        toml_path="pyproject.toml",
    )

    change_flag = False
    for f in data_files:
        flag, dmp_path = dataset(destination=f, json_path=dmp_path, do_print=False)
        if not flag:
            continue
        change_flag = True
        if os.path.exists(".datalad"):
            if set_datalad:
                set_datalad(f)
        elif os.path.exists(".dvc"):
            if set_dvc:
                set_dvc(f)

    try:
        markdown_table, _ = generate_dataset_table(dmp_path, file_descriptions)
        if markdown_table:
            dataset_to_readme(markdown_table=markdown_table, do_print=False)
    except Exception as e:
        print(f"Error: {e}")

    try:
        _ = _sync_sensitive_policy_artifacts_from_dmp(dmp_path)
    except Exception as e:
        print(f"Error syncing sensitive policy artifacts: {e}")

    if (
        change_flag
        and os.path.exists(".git")
        and not os.path.exists(".datalad")
        and not os.path.exists(".dvc")
    ):
        with change_dir(DEFAULT_DATASET_PATH["parent_path"]):
            if os.path.exists(".git"):
                if git_commit:
                    _ = git_commit(msg=git_msg, path=os.getcwd())
                if git_log_to_file:
                    git_log_to_file(os.path.join(".gitlog"))


@ensure_correct_kernel
def main(
    dmp_path: str = DEFAULT_DMP_PATH, do_print: bool = True, git_msg: str = "Running 'set-dataset'"
):
    ensure_project_root()

    os.chdir(PROJECT_ROOT)

    if os.path.exists(".datalad"):
        if datalad_cleaning:
            datalad_cleaning(PROJECT_ROOT)
    elif os.path.exists(".dvc"):
        if dvc_cleaning:
            dvc_cleaning(PROJECT_ROOT)

    DEFAULT_DATASET_PATH, _ = toml_dataset_path()

    data_files, _ = get_data_files(ignore=IGNORE_DICT)

    create_or_update_dmp_from_schema(dmp_path=dmp_path)

    json_path = remove_missing_datasets(json_path=dmp_path)

    if not data_files:
        return

    file_descriptions = read_toml(
        folder=PROJECT_ROOT,
        json_filename="./file_descriptions.json",
        tool_name="file_descriptions",
        toml_path="pyproject.toml",
    )

    change_flag = False
    for f in data_files:
        flag, json_path = dataset(destination=f, json_path=json_path, do_print=do_print)
        if not flag:
            continue
        change_flag = True
        if os.path.exists(".datalad"):
            if set_datalad:
                set_datalad(f)
        elif os.path.exists(".dvc"):
            if set_dvc:
                set_dvc(f)

    try:
        markdown_table, _ = generate_dataset_table(json_path, file_descriptions)
        if markdown_table:
            dataset_to_readme(markdown_table, do_print=do_print)
    except Exception as e:
        print(f"Error: {e}")

    try:
        _ = _sync_sensitive_policy_artifacts_from_dmp(json_path)
    except Exception as e:
        print(f"Error syncing sensitive policy artifacts: {e}")

    if (
        change_flag
        and os.path.exists(".git")
        and not os.path.exists(".datalad")
        and not os.path.exists(".dvc")
    ):
        with change_dir(DEFAULT_DATASET_PATH["parent_path"]):
            if os.path.exists(".git"):
                if git_commit:
                    _ = git_commit(msg=git_msg, path=os.getcwd())
                if git_log_to_file:
                    git_log_to_file(os.path.join(".gitlog"))


if __name__ == "__main__":
    main()
