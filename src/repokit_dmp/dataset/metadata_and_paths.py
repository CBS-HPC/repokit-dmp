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

from .. import bootstrap_runtime_root
from ..dmp import (
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

