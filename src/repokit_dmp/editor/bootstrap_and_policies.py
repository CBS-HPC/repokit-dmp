#!/usr/bin/env python3
"""
Streamlit RDA-DMP JSON editor with per-dataset publish buttons:
- "Publish to Zenodo"
- "Publish to DeiC Dataverse"

Now with autosave: changes are saved to disk automatically whenever fields change.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    import wx
except Exception:
    wx = None


def _find_setup_root() -> Path | None:
    candidates = [Path.cwd(), *Path.cwd().parents]
    here = Path(__file__).resolve()
    candidates.extend([here, *here.parents])
    for p in candidates:
        setup_dir = p / "setup"
        if setup_dir.is_dir():
            return setup_dir
    return None


# --- Robust imports whether run as a package (CLI) or directly via `streamlit run` ---
try:
    from repokit_common import (
        load_from_env,
        save_to_env,
        PROJECT_ROOT,
        read_toml,
        write_toml,
        toml_dataset_path,
        JSON_FILENAME,
        TOOL_NAME,
        TOML_PATH,
    )
    from .. import bootstrap_runtime_root
    from ..dataverse import PublishError, streamlit_publish_to_dataverse
    from ..dataset import dataset_path_update, main as dataset_main
    from ..dmp import (
        DEFAULT_DMP_PATH,
        DK_UNI_MAP,
        EXTRA_ENUMS,
        LICENSE_LINKS,
        SCHEMA_URLS,
        SCHEMA_VERSION,
        dmp_default_templates,
        ensure_dmp_shape,
        ensure_required_by_schema,
        fetch_schema,
        get_repokit_info_payload,
        normalize_datasets_in_place,
        normalize_root_in_place,
        now_iso_minute,
        reorder_dmp_keys,
        repair_empty_enums,
        set_repokit_info_payload,
        today_iso,
        update_cookiecutter_from_dmp,
    )

    # from .publish import *
    from ..zenodo import streamlit_publish_to_zenodo
except ImportError:
    # setup_root = _find_setup_root()
    # if setup_root:
    #    sys.path.insert(0, str(setup_root))
    from repokit_common import (
        load_from_env,
        save_to_env,
        PROJECT_ROOT,
        read_toml,
        write_toml,
        toml_dataset_path,
        JSON_FILENAME,
        TOOL_NAME,
        TOML_PATH,
    )
    from repokit_dmp import bootstrap_runtime_root
    from repokit_dmp.dataverse import PublishError, streamlit_publish_to_dataverse
    from repokit_dmp.dataset import dataset_path_update, main as dataset_main
    from repokit_dmp.dmp import (
        DEFAULT_DMP_PATH,
        DK_UNI_MAP,
        EXTRA_ENUMS,
        LICENSE_LINKS,
        SCHEMA_URLS,
        SCHEMA_VERSION,
        dmp_default_templates,
        ensure_dmp_shape,
        ensure_required_by_schema,
        fetch_schema,
        get_repokit_info_payload,
        normalize_datasets_in_place,
        normalize_root_in_place,
        now_iso_minute,
        reorder_dmp_keys,
        repair_empty_enums,
        set_repokit_info_payload,
        today_iso,
        update_cookiecutter_from_dmp,
    )

    # from repokit_dmp.publish import *
    from repokit_dmp.zenodo import streamlit_publish_to_zenodo


# This is the explicit dependency surface shared by the split editor modules.
__all__ = [
    "DATA_PARENT_PATH",
    "DATAVERSE_SITE_CHOICES",
    "DEFAULT_DMP_PATH",
    "DK_UNI_MAP",
    "EXTRA_ENUMS",
    "JSON_FILENAME",
    "LICENSE_LINKS",
    "PROJECT_ROOT",
    "PublishError",
    "SCHEMA_URLS",
    "SCHEMA_VERSION",
    "TOML_PATH",
    "TOOL_NAME",
    "ZENODO_API_CHOICES",
    "bootstrap_runtime_root",
    "dataset_main",
    "dataset_path_update",
    "dmp_default_templates",
    "ensure_dmp_shape",
    "ensure_required_by_schema",
    "fetch_schema",
    "get_repokit_info_payload",
    "load_from_env",
    "normalize_datasets_in_place",
    "normalize_root_in_place",
    "now_iso_minute",
    "read_toml",
    "reorder_dmp_keys",
    "repair_empty_enums",
    "save_to_env",
    "set_repokit_info_payload",
    "streamlit_publish_to_dataverse",
    "streamlit_publish_to_zenodo",
    "today_iso",
    "toml_dataset_path",
    "update_cookiecutter_from_dmp",
    "write_toml",
    "wx",
]

DATA_PARENT_PATH = Path(".")
# ---------------------------
# Repository site choices (labels come from format_func)
# ---------------------------
ZENODO_API_CHOICES = [
    ("https://sandbox.zenodo.org/api", "Sandbox (highly recommended)"),
    ("https://zenodo.org/api", "Production"),
]

DATAVERSE_SITE_CHOICES = [
    ("https://demo.dataverse.deic.dk", "DeiC Demo (recommended)"),
    ("https://dataverse.deic.dk", "DeiC Production"),
    ("other", "Otherâ€¦"),
]


def _has_privacy_flags(ds: dict) -> bool:
    return (
        str(ds.get("personal_data", "")).lower() == "yes"
        or str(ds.get("sensitive_data", "")).lower() == "yes"
    )


def _is_yes(v: Any) -> bool:
    return str(v or "").strip().lower() == "yes"


def _is_restricted_dataset(ds: dict) -> bool:
    restricted_markers = {"sensitive", "proprietary"}

    def _has_marker(path_like: str) -> bool:
        parts = [p.lower() for p in Path(path_like.replace("\\", "/").lstrip("./")).parts]
        return any(p in restricted_markers for p in parts)

    for dist in ds.get("distribution", []) or []:
        if not isinstance(dist, dict):
            continue
        for key in ("access_url", "download_url"):
            val = dist.get(key)
            if isinstance(val, str) and val.strip() and _has_marker(val):
                return True

    x = get_repokit_info_payload(ds) or {}
    dest = x.get("destination")
    return isinstance(dest, str) and bool(dest.strip()) and _has_marker(dest)


def _enforce_personal_implies_sensitive(ds: dict) -> bool:
    if _is_yes(ds.get("personal_data")) and not _is_yes(ds.get("sensitive_data")):
        ds["sensitive_data"] = "yes"
        return True
    return False


def _enforce_restricted_sensitive_lock(ds: dict) -> bool:
    if _is_restricted_dataset(ds) and not _is_yes(ds.get("sensitive_data")):
        ds["sensitive_data"] = "yes"
        return True
    return False


def _refresh_unblurred_data_files_for_non_sensitive(ds: dict) -> bool:
    """
    If dataset is non-sensitive, rebuild repokit_info.data_files from access path
    when current payload appears redacted/blurred.
    """
    if _has_privacy_flags(ds):
        return False

    x = get_repokit_info_payload(ds) or {}
    current_files = x.get("data_files") or []
    if not isinstance(current_files, list):
        return False

    # Detect current redacted forms:
    # - file_0001.ext
    # - p.........25.csv style masking
    redacted_re = re.compile(r"^file_\d{4}(\.[A-Za-z0-9]+)?$")
    blurred_re = re.compile(r"^[^/\\]\.{3,}[^/\\]*$")
    looks_redacted = all(
        isinstance(f, str) and (redacted_re.match(f) or blurred_re.match(Path(f).name))
        for f in current_files
    )
    if not current_files or not looks_redacted:
        return False

    dists = ds.get("distribution") or []
    access_url = ""
    if isinstance(dists, list):
        for dist in dists:
            if isinstance(dist, dict):
                access_url = (dist.get("access_url") or "").strip()
                if access_url:
                    break
    if not access_url:
        access_url = str(x.get("destination") or "").strip()
    if not access_url:
        return False

    base = Path(access_url)
    if not base.is_absolute():
        base = (PROJECT_ROOT / base).resolve()
    if not base.exists():
        return False

    real_files: list[Path] = []
    if base.is_file():
        real_files = [base]
    else:
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.startswith("."):
                    continue
                real_files.append(Path(root) / fn)

    if not real_files:
        return False

    normalized: list[str] = []
    for fp in sorted(real_files):
        try:
            normalized.append(fp.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix())
        except Exception:
            normalized.append(fp.resolve().as_posix())

    if normalized != current_files:
        x["data_files"] = normalized
        set_repokit_info_payload(ds, x)
        return True
    return False


def _refresh_blurred_data_files_for_sensitive(ds: dict) -> bool:
    """
    If dataset is sensitive/personal, ensure repokit_info.data_files is pseudonymized.
    Uses deterministic file_0001.ext style to match dataset pipeline behavior.
    """
    if not _has_privacy_flags(ds):
        return False

    x = get_repokit_info_payload(ds) or {}
    current_files = x.get("data_files") or []
    if not isinstance(current_files, list):
        return False

    redacted_re = re.compile(r"^file_\d{4}(\.[A-Za-z0-9]+)?$")
    blurred_re = re.compile(r"^[^/\\]\.{3,}[^/\\]*$")

    def _is_redacted_name(item: Any) -> bool:
        if not isinstance(item, str):
            return False
        name = Path(item).name
        return bool(redacted_re.match(name) or blurred_re.match(name))

    # Build source list from current names if they are real; otherwise from filesystem path.
    source_files: list[str] = []
    if current_files and not all(_is_redacted_name(f) for f in current_files):
        source_files = [str(f) for f in current_files if isinstance(f, str) and f.strip()]
    else:
        dists = ds.get("distribution") or []
        access_url = ""
        if isinstance(dists, list):
            for dist in dists:
                if isinstance(dist, dict):
                    access_url = (dist.get("access_url") or "").strip()
                    if access_url:
                        break
        if not access_url:
            access_url = str(x.get("destination") or "").strip()
        if not access_url:
            return False
        base = Path(access_url)
        if not base.is_absolute():
            base = (PROJECT_ROOT / base).resolve()
        if not base.exists():
            return False
        if base.is_file():
            source_files = [base.as_posix()]
        else:
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    if fn.startswith("."):
                        continue
                    source_files.append((Path(root) / fn).as_posix())

    if not source_files:
        return False

    pseudo: list[str] = []
    for idx, p in enumerate(sorted(source_files), start=1):
        suffix = Path(p).suffix.lower()
        pseudo.append(f"file_{idx:04d}{suffix}")

    if pseudo != current_files:
        x["data_files"] = pseudo
        set_repokit_info_payload(ds, x)
        return True
    return False


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
    p = Path(raw)
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


def _upsert_gitignore_patterns(gitignore_path: Path, entries: list[str]) -> bool:
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


def _sync_sensitive_policy_artifacts(datasets: list[dict[str, Any]]) -> bool:
    sensitive_rel_paths: list[str] = []
    seen: set[str] = set()
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        if not _is_yes(ds.get("sensitive_data")):
            continue
        rel = _to_project_relative(_dataset_primary_path(ds))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        sensitive_rel_paths.append(rel)

    if not sensitive_rel_paths:
        return False

    changed = False

    # 1) Root .gitignore
    root_entries = [_gitignore_entry(p) for p in sensitive_rel_paths]
    changed |= _upsert_gitignore_patterns(PROJECT_ROOT / ".gitignore", root_entries)
    changed |= _upsert_agent_ignore_patterns(root_entries)

    # 2) /data .gitignore if /data is its own git repo
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

    # 3) pyproject [tool.data_policy].patterns
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
