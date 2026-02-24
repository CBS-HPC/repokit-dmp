#!/usr/bin/env python3
"""
Streamlit RDA-DMP JSON editor with per-dataset publish buttons:
- "Publish to Zenodo"
- "Publish to DeiC Dataverse"

Now with autosave: changes are saved to disk automatically whenever fields change.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256  # <- for autosave hashing
from pathlib import Path
from typing import Any
try:
    import wx
except Exception:
    wx = None

import requests
import streamlit as st
from streamlit.web.cli import main as st_main


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
    from . import ensure_project_root
    from .dataverse import PublishError, streamlit_publish_to_dataverse
    from .dataset import dataset_path_update, main as dataset_main
    from .dmp import (
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
    from .zenodo import streamlit_publish_to_zenodo
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
    from repokit_dmp import ensure_project_root
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
    ("other", "Other…"),
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

    real_files: list[str] = []
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


def _normalize_chosen_path(chosen: str) -> str:
    """
    If `chosen` is under PROJECT_ROOT, return a path relative to PROJECT_ROOT.
    Otherwise, return an absolute POSIX-style path.

    Examples:
      PROJECT_ROOT = /home/user/project
      chosen = /home/user/project/data/raw/file.csv  -> "data/raw/file.csv"
      chosen = /other/place/file.csv                 -> "/other/place/file.csv"
    """
    s = (chosen or "").strip()
    if not s:
        return s

    p = Path(s)

    # If it's already relative, just normalise separators
    if not p.is_absolute():
        return p.as_posix()

    # Try to relativate to PROJECT_ROOT if available
    try:
        root = PROJECT_ROOT.resolve()
    except Exception:
        # PROJECT_ROOT not defined / not resolvable: keep as absolute
        return p.resolve().as_posix()

    try:
        rel = p.resolve().relative_to(root)
        # Subpath of PROJECT_ROOT → keep relative
        return rel.as_posix()
    except ValueError:
        # Not under PROJECT_ROOT → keep absolute
        return p.resolve().as_posix()


def _enforce_privacy_access(ds: dict) -> bool:
    """If personal/sensitive == yes, force all distributions to closed."""
    changed = False
    for dist in ds.get("distribution", []) or []:
        if _has_privacy_flags(ds) and dist.get("data_access") != "closed":
            dist["data_access"] = "closed"
            changed = True
    return changed


def _normalize_license_by_access(ds: dict) -> bool:
    """If data_access is shared/closed, remove CC license URLs (misleading for non-open)."""
    changed = False
    for dist in ds.get("distribution", []) or []:
        access = (dist.get("data_access") or "").lower()
        if access in {"shared", "closed"}:
            for lic in dist.get("license", []) or []:
                ref = (lic or {}).get("license_ref") or ""
                if "creativecommons.org" in ref:
                    lic["license_ref"] = ""
                    changed = True
    return changed


def _ensure_open_has_license(ds: dict) -> bool:
    """If open and license empty, set default CC-BY-4.0."""
    changed = False
    default_ref = LICENSE_LINKS.get("CC-BY-4.0", "")
    for dist in ds.get("distribution", []) or []:
        if (dist.get("data_access") or "").lower() == "open":
            lics = dist.get("license") or []
            if not lics:
                dist["license"] = [{"license_ref": default_ref, "start_date": today_iso()}]
                changed = True
            else:
                for lic in lics:
                    if not (lic or {}).get("license_ref"):
                        lic["license_ref"] = default_ref
                        changed = True
    return changed


def _inject_dist_css_once() -> None:
    if st.session_state.get("__dist_css__"):
        return
    st.session_state["__dist_css__"] = True
    st.markdown(
        """
        <style>
        [data-testid="stExpander"] > div {
            border-left: 3px solid #c8d6e5;
            background: rgba(0,0,0,.02);
            padding: .6rem 1rem .8rem 1rem;
            border-radius: 6px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _is_dataset_path(path: tuple) -> bool:
    return (
        isinstance(path, tuple)
        and len(path) == 3
        and path[0] == "dmp"
        and path[1] == "dataset"
        and isinstance(path[2], int)
    )


def _edit_distribution_inline(arr: list[Any], path: tuple, ns: str | None = None) -> list[Any]:
    if not isinstance(arr, list):
        return arr
    if not arr:
        templates = dmp_default_templates()
        arr.append(deepcopy(templates["distribution"]))
    if len(arr) == 1 and isinstance(arr[0], dict):
        _inject_dist_css_once()
        with st.expander("Distribution", expanded=True):
            arr[0] = edit_any(arr[0], path=(*path, 0), ns=ns)
        return arr
    return edit_array(arr, path=path, title_singular="Distribution", removable_items=True, ns=ns)


def _edit_dataset_id_inline(obj: Any, path: tuple, ns: str | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict) or not obj:
        templates = dmp_default_templates()
        obj = deepcopy(templates["dataset"]["dataset_id"])
    with st.expander("Dataset ID", expanded=True):
        obj = edit_any(obj, path=path, ns=ns)
    return obj


def _ensure_dataset_id_before_distribution(keys: list[str]) -> list[str]:
    if "dataset_id" in keys and "distribution" in keys:
        di, dj = keys.index("dataset_id"), keys.index("distribution")
        if di > dj:
            k = keys.pop(di)
            keys.insert(dj, k)
    return keys


# ──────────────────────────────────────────────────────────────────────────────
# Minimal helpers (schema-aware editors)
# ──────────────────────────────────────────────────────────────────────────────


def _key_for(*parts: Any) -> str:
    return "|".join(str(p) for p in parts if p is not None)


def _parse_iso_date(s: Any) -> date | None:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, str) and s:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _get_schema_cached() -> dict[str, Any] | None:
    key = "__rda_schema__"
    if key in st.session_state:
        return st.session_state[key]
    try:
        sch = fetch_schema()
        st.session_state[key] = sch
        return sch
    except Exception:
        st.session_state[key] = None
        return None


def safe_fetch_schema() -> dict[str, Any] | None:
    return _get_schema_cached()


def _schema_node_for_path(path: tuple) -> dict[str, Any] | None:
    schema = safe_fetch_schema()
    if not schema:
        return None

    def _resolve_ref(n: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(n, dict):
            return {}
        if "$ref" in n:
            ref = n["$ref"]
            if not (isinstance(ref, str) and ref.startswith("#/")):
                return {}
            cur: Any = schema
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                cur = cur.get(part, {})
            n = cur if isinstance(cur, dict) else {}
        return n

    node: dict[str, Any] = schema
    for comp in path:
        node = _resolve_ref(node)
        if isinstance(comp, str):
            props = node.get("properties")
            if isinstance(props, dict) and comp in props:
                node = props[comp]
                continue
            if node.get("type") == "array" and isinstance(node.get("items"), dict):
                items = _resolve_ref(node["items"])
                props = items.get("properties")
                if isinstance(props, dict) and comp in props:
                    node = props[comp]
                    continue
            return None
        else:
            if node.get("type") == "array" and isinstance(node.get("items"), dict):
                node = node["items"]
                continue
            return None
    return _resolve_ref(node)


def _is_format_schema(path: tuple, fmt: str) -> bool:
    node = _schema_node_for_path(path)
    return bool(node and node.get("type") == "string" and node.get("format") == fmt)


def _is_boolean_schema(path: tuple) -> bool:
    node = _schema_node_for_path(path)
    return bool(node and node.get("type") == "boolean")


def _coerce_to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


STRING_LIST_HINTS = {"data_quality_assurance", "keyword", "format", "pid_system", "role"}


def _looks_like_string_list(arr: list[Any], path: tuple) -> bool:
    if arr and all(isinstance(x, str) for x in arr):
        return True
    return (not arr) and bool(path) and (path[-1] in STRING_LIST_HINTS)


def _path_signature(path: tuple) -> str:
    parts: list[str] = []
    for p in path:
        if isinstance(p, int):
            if parts:
                parts[-1] = parts[-1] + "[]"
            else:
                parts.append("[]")
        else:
            parts.append(str(p))
    return ".".join(parts)


def _enum_info_for_path(path: tuple):
    node = _schema_node_for_path(path)
    base_mode: str | None = None
    base_options: list[str] = []
    if node:
        if node.get("type") == "string" and isinstance(node.get("enum"), list):
            base_mode = "single"
            base_options = list(node["enum"])
        elif node.get("type") == "array":
            it = node.get("items")
            if (
                isinstance(it, dict)
                and it.get("type") == "string"
                and isinstance(it.get("enum"), list)
            ):
                base_mode = "multi"
                base_options = list(it["enum"])
    sig = _path_signature(path)
    try:
        extras = EXTRA_ENUMS.get(sig, [])  # type: ignore[name-defined]
    except Exception:
        extras = []
    extra_values: list[str] = []
    if isinstance(extras, list):
        if extras and isinstance(extras[0], dict):
            extra_values = [
                d.get("value", "") for d in extras if isinstance(d, dict) and d.get("value")
            ]
        else:
            extra_values = [str(x) for x in extras]
    inferred_mode: str | None = None
    if node and node.get("type") == "array":
        inferred_mode = "multi"
    elif node and node.get("type") == "string" or base_options or extra_values:
        inferred_mode = "single"
    merged = list(dict.fromkeys(base_options + extra_values))
    if not merged:
        return (None, [])
    return (base_mode or inferred_mode or "single", merged)


def _enum_label_for(path: tuple, option_value: str) -> str:
    sig = _path_signature(path)
    try:
        extras = EXTRA_ENUMS.get(sig, [])  # type: ignore[name-defined]
    except Exception:
        extras = []
    if isinstance(extras, list):
        for d in extras:
            if isinstance(d, dict) and d.get("value") == option_value:
                return d.get("label", option_value)
    return str(option_value)


def edit_primitive(label: str, value: Any, path: tuple, ns: str | None = None) -> Any:
    # Special handling for is_reused: use dropdown instead of checkbox
    if path and path[-1] == "is_reused":
        key = _key_for(*path, ns, "is_reused_select")
        current = "yes" if _coerce_to_bool(value) else "no"
        selected = st.selectbox(
            label,
            options=["no", "yes"],
            index=0 if current == "no" else 1,
            key=key,
            help="Select 'yes' if this dataset reuses existing data",
        )
        return selected == "yes"

    if _is_boolean_schema(path):
        keyb = _key_for(*path, ns, "bool")
        return st.checkbox(label, value=_coerce_to_bool(value), key=keyb)

    if _is_format_schema(path, "date"):
        base_key = _key_for(*path, ns, "date")
        enable_key = _key_for(*path, ns, "date_enabled")
        pending_key = _key_for(*path, ns, "date_pending")
        set_key = _key_for(*path, ns, "date_set_btn")
        clear_key = _key_for(*path, ns, "date_clear_btn")

        existing = _parse_iso_date(value)
        if enable_key not in st.session_state:
            st.session_state[enable_key] = bool(existing)
        enabled = bool(st.session_state[enable_key])

        c1, c2 = st.columns([4, 1])
        with c2:
            st.markdown("<div style='height:0.15rem'></div>", unsafe_allow_html=True)
            if enabled:
                has_date_now = (base_key in st.session_state) or bool(existing)
                if st.button("Clear", key=clear_key, disabled=not has_date_now):
                    st.session_state.pop(base_key, None)
                    st.session_state[enable_key] = False
                    enabled = False
            else:
                if st.button("Set date", key=set_key):
                    st.session_state[enable_key] = True
                    st.session_state[pending_key] = True
                    enabled = True
        with c1:
            seed_today = bool(st.session_state.pop(pending_key, False))
            cur = (
                date.today()
                if (seed_today and not existing)
                else (st.session_state.get(base_key) or existing or date.today())
            )
            picked = st.date_input(label, value=cur, key=base_key, disabled=not enabled)
        return "" if not enabled else (picked.isoformat() if isinstance(picked, date) else "")

    mode, options = _enum_info_for_path(path)
    if mode == "single" and options:
        sel_key = _key_for(*path, ns, "enum")
        options_ui = list(options)
        custom_label = None
        if value not in (None, "") and value not in options_ui:
            custom_label = f"(custom) {value}"
            options_ui = [custom_label] + options_ui
            default_index = 0
        else:
            try:
                default_index = options_ui.index(value)
            except Exception:
                default_index = 0
        selected = st.selectbox(
            label,
            options_ui,
            index=default_index,
            key=sel_key,
            format_func=lambda opt: _enum_label_for(path, opt if opt != custom_label else value),
        )
        return value if (custom_label and selected == custom_label) else selected

    key = _key_for(*path, ns, "prim")
    if isinstance(value, bool):
        return st.checkbox(label, value=value, key=key)
    if isinstance(value, int):
        txt = st.text_input(label, str(value), key=key)
        try:
            return int(txt) if txt != "" else None
        except Exception:
            return value
    if isinstance(value, float):
        txt = st.text_input(label, str(value), key=key)
        try:
            return float(txt) if txt != "" else None
        except Exception:
            return value
    txt = st.text_input(label, "" if value is None else str(value), key=key)
    return txt


def edit_array(
    arr: list[Any],
    path: tuple,
    title_singular: str,
    removable_items: bool,
    ns: str | None = None,
) -> list[Any]:
    mode, options = _enum_info_for_path(path)
    if mode == "multi" and options:
        label = f"{path[-1] if path else title_singular} (choose any)"
        wkey = _key_for(*path, ns, "enum_multi")
        current = [x for x in arr if isinstance(x, str) and x in options]
        selected = st.multiselect(
            label,
            options,
            default=current,
            key=wkey,
            format_func=lambda opt: _enum_label_for(path, opt),
        )
        return selected
    if _looks_like_string_list(arr, path):
        label = f"{path[-1] if path else title_singular} (one per line; saved as array)"
        key = _key_for(*path, ns, "textlist")
        initial = "\n".join(x for x in arr if isinstance(x, str))
        txt = st.text_area(label, initial, key=key)
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        return lines
    if len(arr) == 1 and isinstance(arr[0], dict):
        label = path[-1] if path else title_singular
        st.caption(f"{label} — single entry")
        arr[0] = edit_any(arr[0], path=(*path, 0), ns=ns)
        if removable_items:
            if st.button(
                f"🗑️ Remove this {title_singular.lower()}", key=_key_for(*path, ns, "rm_single")
            ):
                arr.clear()
                # st.success(f"{title_singular} removed")
                st.rerun()
        return arr

    # Track deletion separately - don't modify list while iterating
    item_to_delete = None

    for i, item in enumerate(list(arr)):
        heading = f"{title_singular} #{i + 1}"
        if isinstance(item, dict):
            for pick in ("title", "name", "identifier"):
                if item.get(pick):
                    heading = f"{title_singular} #{i + 1}: {item[pick]}"
                    break
        with st.expander(heading, expanded=False):
            # Only edit if we're not deleting this item
            if item_to_delete != i:
                arr[i] = edit_any(item, path=(*path, i), ns=ns)

            if removable_items:
                if st.button(
                    f"🗑️ Remove this {title_singular.lower()}", key=_key_for(*path, i, ns, "rm")
                ):
                    item_to_delete = i

    # Perform deletion after iteration is complete
    if item_to_delete is not None:
        del arr[item_to_delete]
        # st.success(f"{title_singular} #{item_to_delete + 1} removed")
        st.rerun()

    return arr


# Read-only JSON helper for repokit_info
def _is_under_dataset_extension(path: tuple) -> bool:
    return (
        isinstance(path, tuple)
        and len(path) >= 5
        and path[0] == "dmp"
        and path[1] == "dataset"
        and isinstance(path[2], int)
        and path[3] == "extension"
    )


def _show_readonly_json(label: str, value: Any, key: str | None = None) -> None:
    import json as _json

    with st.expander(label, expanded=False):
        try:
            st.code(_json.dumps(value, indent=2, ensure_ascii=False), language="json")
        except Exception:
            st.code(str(value), language="json")


def _is_empty_alias(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def edit_object(
    obj: dict[str, Any], path: tuple, allow_remove_keys: bool, ns: str | None = None
) -> dict[str, Any]:
    keys = list(obj.keys())
    if _is_dataset_path(path):
        keys = _ensure_dataset_id_before_distribution(keys)

    remove_keys: list[str] = []
    for key in keys:
        val = obj.get(key)

        if path == ("dmp",) and key in ("project", "dataset", "contributor"):
            continue

        if key == "repokit_info" and _is_under_dataset_extension(path):
            _show_readonly_json("repokit_info (read-only)", val, key=_key_for(*path, key, ns, "ro"))
            continue

        if key == "extension" and isinstance(val, list):
            with st.expander("extension", expanded=False):
                obj["extension"] = edit_array(
                    val,
                    path=(*path, "extension"),
                    title_singular="Entry",
                    removable_items=True,
                    ns=ns,
                )
            continue

        if isinstance(val, dict):
            if key == "dataset_id" and _is_dataset_path(path):
                obj[key] = _edit_dataset_id_inline(val, path=(*path, key), ns=ns)
                continue
            with st.expander(key, expanded=False):
                obj[key] = edit_any(val, path=(*path, key), ns=ns)

        elif isinstance(val, list):
            if key == "distribution" and _is_dataset_path(path):
                obj[key] = _edit_distribution_inline(val, path=(*path, key), ns=ns)
                continue
            with st.expander(key, expanded=False):
                title = "Distribution" if key == "distribution" else "Item"
                obj[key] = edit_array(
                    val, path=(*path, key), title_singular=title, removable_items=False, ns=ns
                )

        else:
            if key == "sensitive_data" and _is_dataset_path(path):
                field_path = (*path, key)
                restricted = _is_restricted_dataset(obj)
                mode, options = _enum_info_for_path(field_path)
                if mode == "single" and options:
                    sel_key = _key_for(*field_path, ns, "enum")
                    current = "" if val is None else str(val)
                    options_ui = list(options)
                    custom_label = None
                    if current not in (None, "") and current not in options_ui:
                        custom_label = f"(custom) {current}"
                        options_ui = [custom_label] + options_ui
                        default_index = 0
                    else:
                        try:
                            default_index = options_ui.index(current)
                        except Exception:
                            default_index = 0
                    selected = st.selectbox(
                        key,
                        options_ui,
                        index=default_index,
                        key=sel_key,
                        disabled=restricted,
                        format_func=lambda opt: _enum_label_for(
                            field_path, opt if opt != custom_label else current
                        ),
                    )
                    obj[key] = current if (custom_label and selected == custom_label) else selected
                else:
                    obj[key] = edit_primitive(key, val, path=field_path, ns=ns)

                if restricted:
                    st.caption(
                        "Sensitive flag is locked to 'yes' for datasets under /sensitive or /proprietary."
                    )
            else:
                obj[key] = edit_primitive(key, val, path=(*path, key), ns=ns)

        if allow_remove_keys and st.button(
            f"Remove key: {key}", key=_key_for(*path, key, ns, "del")
        ):
            remove_keys.append(key)

    for k in remove_keys:
        obj.pop(k, None)
    return obj


def edit_any(value: Any, path: tuple, ns: str | None = None) -> Any:
    if isinstance(value, dict):
        return edit_object(value, path, allow_remove_keys=False, ns=ns)
    if isinstance(value, list):
        return edit_array(value, path, title_singular="Item", removable_items=False, ns=ns)
    return edit_primitive("value", value, path, ns=ns)


# ──────────────────────────────────────────────────────────────────────────────
# High-level sections (Root, Projects, Datasets)
# ──────────────────────────────────────────────────────────────────────────────


def find_default_dmp_path(start: Path | None = None) -> Path:
    launch_root_raw = os.environ.get("REPOKIT_DMP_PROJECT_ROOT", "").strip()
    if launch_root_raw:
        return (Path(launch_root_raw).resolve() / "dmp.json").resolve()
    return (PROJECT_ROOT / "dmp.json").resolve()


def draw_root_section(dmp_root: dict[str, Any]) -> None:
    st.subheader("Root")
    dmp_root.setdefault("schema", dmp_root.get("schema") or SCHEMA_URLS[SCHEMA_VERSION])
    dmp_root.setdefault(
        "contact",
        dmp_root.get("contact")
        or {
            "name": "",
            "mbox": "",
            "contact_id": {"type": "orcid", "identifier": ""},
        },
    )

    templates = dmp_default_templates() if "dmp_default_templates" in globals() else {}
    default_contrib = (
        deepcopy(templates.get("contributor"))
        if templates and "contributor" in templates
        else {
            "name": "",
            "mbox": "",
            "contributor_id": {"type": "orcid", "identifier": ""},
            "role": [],
        }
    )

    for key in list(dmp_root.keys()):
        if key in ("project", "dataset"):
            continue
        val = dmp_root.get(key)
        if isinstance(val, dict):
            with st.expander(key, expanded=False):
                dmp_root[key] = edit_any(val, path=("dmp", key), ns=None)
        elif isinstance(val, list):
            with st.expander(key, expanded=False):
                dmp_root[key] = edit_array(
                    val, path=("dmp", key), title_singular="Item", removable_items=False, ns=None
                )
        else:
            dmp_root[key] = edit_primitive(key, val, path=("dmp", key), ns=None)

    add_col, _ = st.columns([1, 6])
    with add_col:
        if st.button("➕ Add contributor", key=_key_for("dmp", "contributor", "add", "bottom")):
            contribs = dmp_root.setdefault("contributor", [])
            contribs.append(deepcopy(default_contrib))
            st.rerun()


def draw_projects_section(dmp_root: dict[str, Any]) -> None:
    st.subheader("Projects")
    projects = dmp_root.get("project")
    if not isinstance(projects, list):
        projects = []
        dmp_root["project"] = projects

    cols = st.columns(2)
    templates = dmp_default_templates()
    with cols[0]:
        if st.button("➕ Add project", key=_key_for("project", "add")):
            projects.append(templates["project"])
            st.rerun()

    # Track deletion separately
    project_to_delete = None

    for i, proj in enumerate(projects):
        # edit_any for projects uses ns=None, so edit_primitive keys are:
        # _key_for("dmp", "project", i, "title", "prim")
        title_key = _key_for("dmp", "project", i, "title", "prim")
        live_title = st.session_state.get(title_key, proj.get("title") or proj.get("name"))
        header_title = (
            live_title or proj.get("title") or proj.get("name") or "Project"
        ).strip() or "Project"

        with st.expander(f"Project #{i + 1}: {header_title}", expanded=False):
            projects[i] = edit_any(proj, path=("dmp", "project", i), ns=None)

            if st.button(
                "🗑️ Remove this project",
                key=_key_for("dmp", "project", i, "rm"),
            ):
                project_to_delete = i

    if project_to_delete is not None:
        del projects[project_to_delete]
        st.rerun()

    dmp_root["project"] = projects


def _browse_for_directory(
    start_path: str | Path | None = None,
    title: str = "Select a folder",
    dir_only: bool = True,
) -> str | None:
    """
    Open a native chooser dialog using wxPython.

    Args:
        start_path: Initial folder or file path to start from.
        title:      Dialog title.
        dir_only:   If True → choose a directory.
                    If False → choose a single file.

    Returns:
        Selected path as a string (directory or file, depending on dir_only),
        or None if cancelled.
    """
    # Normalise starting path
    if start_path:
        start_str = os.fspath(start_path)
    else:
        start_str = os.getcwd()

    # Compute default directory / file
    if dir_only:
        default_dir = start_str
        if os.path.isfile(default_dir):
            default_dir = os.path.dirname(default_dir)
        default_file = ""
    else:
        if os.path.isfile(start_str):
            default_dir, default_file = os.path.split(start_str)
        else:
            default_dir, default_file = (start_str, "")

    # Preferred: native chooser via wxPython
    if wx is not None:
        app = wx.App(False)
        if dir_only:
            dlg = wx.DirDialog(
                None,
                message=title,
                defaultPath=default_dir,
                style=wx.DD_DEFAULT_STYLE | wx.DD_NEW_DIR_BUTTON,
            )
        else:
            dlg = wx.FileDialog(
                None,
                message=title,
                defaultDir=default_dir,
                defaultFile=default_file,
                wildcard="*.*",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            )
        try:
            if dlg.ShowModal() == wx.ID_OK:
                return dlg.GetPath()
            return None
        finally:
            dlg.Destroy()
            app.Destroy()

    # Fallback: tkinter (standard library)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if dir_only:
                selected = filedialog.askdirectory(initialdir=default_dir, title=title)
            else:
                selected = filedialog.askopenfilename(
                    initialdir=default_dir,
                    initialfile=default_file,
                    title=title,
                )
            return selected or None
        finally:
            root.destroy()
    except Exception:
        st.warning(
            "Path picker unavailable (missing wxPython/tkinter GUI backend). "
            "Install wxPython or set paths directly in dmp.json."
        )
        return None


def _reload_dmp_from_disk(
    save_path: Path,
    clear_widget_keys: bool = True,
    reset_autosave_baseline: bool = True,
    force_widget_refresh: bool = False,
    rerun: bool = False,
) -> None:
    """
    Reload the DMP JSON from disk into session_state["data"], optionally
    clearing Streamlit widget keys and resetting the autosave baseline.

    If force_widget_refresh=True, increment a counter to force all widgets to recreate.
    If rerun=True, calls st.rerun() at the end.
    """
    try:
        with save_path.open("r", encoding="utf-8") as f:
            reloaded = json.load(f)
        st.session_state["data"] = ensure_dmp_shape(reloaded)
    except Exception as e:
        st.error(f"Failed to reload updated DMP: {e}")
        # On a hard failure, don't try to clear keys or rerun
        return

    if clear_widget_keys:
        # Clear all DMP-related widget keys so they pick up reloaded values
        keys_to_clear = [
            k
            for k in list(st.session_state.keys())
            if isinstance(k, str) and (k.startswith("dmp|") or k.startswith("deep|"))
        ]
        for k in keys_to_clear:
            del st.session_state[k]

    if reset_autosave_baseline:
        st.session_state.pop("__autosave_last_hash__", None)

    if force_widget_refresh:
        # Increment counter to force all widgets to recreate with new keys
        counter = st.session_state.get("__widget_refresh_counter__", 0)
        st.session_state["__widget_refresh_counter__"] = counter + 1

    if rerun:
        st.rerun()


def draw_datasets_section(dmp_root: dict) -> None:
    st.subheader("Datasets")

    # Get widget refresh counter for forcing widget recreation
    widget_version = st.session_state.get("__widget_refresh_counter__", 0)

    datasets = dmp_root.get("dataset")
    if not isinstance(datasets, list):
        datasets = []
        dmp_root["dataset"] = datasets

    # --- Top row: Add dataset + Parent Data Path + Change Path ---
    top = st.columns([1, 4, 1])
    templates = dmp_default_templates()

    # Initialize parent data path in session (for later override)
    if "__parent_data_path__" not in st.session_state:
        st.session_state["__parent_data_path__"] = str(DATA_PARENT_PATH)

    parent_data_path = st.session_state["__parent_data_path__"]

    with top[0]:
        if st.button("➕ Add dataset", key=_key_for("dataset", "add")):
            new_index = len(datasets) + 1
            new_ds = deepcopy(templates["dataset"])
            if isinstance(new_ds, dict):
                title_val = str(new_ds.get("title") or "").strip()
                if not title_val:
                    new_ds["title"] = f"Dataset {new_index}"

            datasets.append(new_ds)
            dmp_root["dataset"] = datasets

            if "data" in st.session_state and isinstance(st.session_state["data"], dict):
                st.session_state["data"].setdefault("dmp", {})
                st.session_state["data"]["dmp"] = dmp_root

            _autosave_if_changed(force_write=True)
            st.rerun()

    with top[1]:
        c_label, c_input = st.columns([0.4, 4])
        with c_label:
            st.caption("Parent Data Path")
        with c_input:
            st.text_input(
                "Parent Data Path",
                value=parent_data_path,
                key=f"parent_data_path_display_v{widget_version}",
                disabled=True,
                label_visibility="collapsed",
            )

    with top[2]:
        if st.button("Change Path", key="change_parent_data_path"):
            # Start from current parent_data_path (or fallback)
            start_path = parent_data_path or st.session_state.get(
                "__parent_data_path__", str(DATA_PARENT_PATH)
            )

            chosen = _browse_for_directory(
                start_path=start_path,
                title="Select parent data folder for datasets",
                dir_only=True,
            )

            if chosen:
                chosen_norm = _normalize_chosen_path(chosen)

                # 1) Update in-memory parent path used by the UI
                st.session_state["__parent_data_path__"] = chosen_norm

                try:
                    # 2) Let the central autosave logic write the DMP to disk
                    _autosave_if_changed(force_write=True)

                    # Resolve the path that autosave wrote to
                    save_path_str = st.session_state.get("save_path") or str(DEFAULT_DMP_PATH)
                    save_path = Path(save_path_str).resolve()

                    # 3) Persist to TOML
                    write_toml(
                        data={"patterns": [chosen_norm]},
                        folder=str(PROJECT_ROOT),
                        json_filename=JSON_FILENAME,
                        tool_name="datasets",
                        toml_path=TOML_PATH,
                    )

                    # 4) Rebuild dataset metadata
                    try:
                        dataset_main(
                            dmp_path=save_path,
                            do_print=False,
                            git_msg=f"Setting parent dataset path to {chosen_norm}",
                        )
                    except Exception as e:
                        st.warning(f"dataset_main failed: {e}")

                    # 5) Reload DMP + force widget refresh + reset UI / autosave baseline, then rerun
                    _reload_dmp_from_disk(
                        save_path,
                        clear_widget_keys=True,
                        reset_autosave_baseline=True,
                        force_widget_refresh=True,
                        rerun=True,
                    )
                except Exception as e:
                    st.error(f"Failed to save parent data path: {e}")
                else:
                    st.rerun()

    def _is_unknown(v: Any) -> bool:
        s = str(v or "").strip().lower()
        return s in {"unknown", "unkown", ""}

    def _first_access_url(ds: dict) -> str:
        """Return the first non-empty access_url from distribution, or ''."""
        dists = ds.get("distribution") or []
        if not isinstance(dists, list):
            return ""
        for dist in dists:
            if isinstance(dist, dict):
                url = (dist.get("access_url") or "").strip()
                if url:
                    return url
        return ""

    # Track only reuse changes – deletion is now inline
    is_reused_changed = False

    for i, ds in enumerate(datasets):
        # Use versioned keys for widgets that need to refresh
        title_key = f"dmp|dataset|{i}|title|deep|prim_v{widget_version}"
        live_title = st.session_state.get(title_key, ds.get("title"))
        header_title = (live_title or ds.get("title") or "Dataset").strip() or "Dataset"

        with st.expander(f"Dataset #{i + 1}: {header_title}", expanded=False):
            prev_is_reused = _is_reused(ds)

            is_reused = _is_reused(ds)
            has_unknown_privacy = _is_unknown(ds.get("personal_data")) or _is_unknown(
                ds.get("sensitive_data")
            )

            override_key = f"allow_reused_{i}_v{widget_version}"
            allow_override = st.session_state.get(override_key, False)
            if not is_reused and allow_override:
                st.session_state[override_key] = False
                allow_override = False

            zen_disabled = (is_reused and not allow_override) or has_unknown_privacy

            site_choice = st.session_state.get("dataverse_site_choice", "")
            alias_effective = st.session_state.get("dataverse_alias", "") or _get_env_or_secret(
                "DATAVERSE_ALIAS", ""
            )
            if site_choice != "other" and _is_empty_alias(alias_effective):
                _gb, _ga = _guess_dataverse_defaults_from_university(st.session_state.get("data"))
                if _ga:
                    alias_effective = _ga

            alias_missing = _is_empty_alias(alias_effective)
            dv_disabled = (is_reused and not allow_override) or has_unknown_privacy or alias_missing

            # Layout row
            cols = st.columns([1, 1, 1, 2, 4])

            # --- INLINE DELETE: remove immediately on click ---
            with cols[0]:
                if st.button("🗑️ Remove this dataset", key=f"rm_ds_{i}_v{widget_version}"):
                    # Delete the dataset at index i right away
                    del datasets[i]
                    dmp_root["dataset"] = datasets

                    if "data" in st.session_state and isinstance(st.session_state["data"], dict):
                        st.session_state["data"].setdefault("dmp", {})
                        st.session_state["data"]["dmp"] = dmp_root

                    # Clean up any reuse-override state for this index
                    st.session_state.pop(override_key, None)

                    # Persist changes and rerun
                    _autosave_if_changed(force_write=True)
                    st.rerun()

            with cols[1]:
                if st.button(
                    "Publish to Zenodo",
                    key=f"pub_zen_{i}_v{widget_version}",
                    disabled=zen_disabled,
                ):
                    token = get_token_from_state("zenodo")
                    if not token:
                        st.warning("Please set a Zenodo token in the sidebar and press Save.")
                        st.stop()
                    try:
                        streamlit_publish_to_zenodo(
                            dataset=ds,
                            dmp=st.session_state["data"],
                            token=token,
                            base_url=get_zenodo_api_base(),
                            community=get_zenodo_community(),
                            publish=False,
                            allow_reused=allow_override,
                        )
                    except PublishError as e:
                        st.error(str(e))

            with cols[2]:
                if st.button(
                    "Publish to DeiC Dataverse",
                    key=f"pub_dv_{i}_v{widget_version}",
                    disabled=dv_disabled,
                ):
                    token = get_token_from_state("dataverse")
                    if not token:
                        st.warning("Please set a Dataverse token in the sidebar and press Save.")
                        st.stop()
                    try:
                        dv_base, dv_alias = get_dataverse_config()
                        if not dv_base:
                            st.warning(
                                "Please select a Dataverse base URL in the sidebar and press Save."
                            )
                            st.stop()
                        if _is_empty_alias(dv_alias):
                            st.warning(
                                "Please enter a Dataverse collection (alias) in the sidebar and press Save."
                            )
                            st.stop()
                        streamlit_publish_to_dataverse(
                            dataset=ds,
                            dmp=st.session_state["data"],
                            token=token,
                            base_url=dv_base,
                            alias=dv_alias,
                            publish=False,
                            release_type="major",
                            allow_reused=allow_override,
                        )
                    except PublishError as e:
                        st.error(str(e))

                if alias_missing:
                    if site_choice == "other":
                        st.caption(
                            "⚠️ Enter a Dataverse collection (alias) in the sidebar to enable publishing."
                        )
                    else:
                        st.caption(
                            "⚠️ No collection (alias) detected. We'll try to infer it, or set one in the sidebar."
                        )

            with cols[3]:
                st.checkbox(
                    "Override reuse restriction",
                    key=override_key,
                    value=allow_override,
                    disabled=not is_reused,
                    help=(
                        "Enable publishing even when 'is_reused' is set to 'yes'. "
                        "Only available when dataset is marked as reused."
                    ),
                )

            # Data path display + button
            with cols[4]:
                access_url = _first_access_url(ds)
                btn_label = "Change Data Path" if access_url else "Add Data Path"
                subcol_path, subcol_btn = st.columns([3, 1])

                with subcol_path:
                    st.caption("Data path")
                    # Use versioned key and always read from current data
                    preview_key = f"dmp|dataset|{i}|data_path_preview_v{widget_version}"
                    st.text_input(
                        "Data path",
                        value=access_url or "",
                        key=preview_key,
                        disabled=True,
                        label_visibility="collapsed",
                    )

                with subcol_btn:
                    if st.button(
                        btn_label,
                        key=f"dmp|dataset|{i}|data_path_action_v{widget_version}",
                    ):
                        # 1) Let user pick a folder or file(s)
                        start_path = access_url or st.session_state.get(
                            "__parent_data_path__", str(DATA_PARENT_PATH)
                        )
                        chosen = _browse_for_directory(
                            start_path=start_path,
                            title=f"Select data folder/files for dataset #{i + 1}",
                            dir_only=False,
                        )

                        if chosen:
                            chosen_norm = _normalize_chosen_path(chosen)

                            # 2) Update in-memory DMP for this dataset
                            dists = ds.setdefault("distribution", [])
                            if not dists or not isinstance(dists[0], dict):
                                if dists and not isinstance(dists[0], dict):
                                    dists.clear()
                                dists.append({})
                            dists[0]["access_url"] = chosen_norm

                            # Make sure the change is reflected in the datasets list
                            datasets[i] = ds
                            dmp_root["dataset"] = datasets

                            # And in the top-level DMP object in session_state
                            if "data" in st.session_state and isinstance(
                                st.session_state["data"], dict
                            ):
                                st.session_state["data"].setdefault("dmp", {})
                                st.session_state["data"]["dmp"] = dmp_root

                            try:
                                # 3) Let the central autosave logic write the DMP to disk
                                _autosave_if_changed(force_write=True)

                                # Resolve the path that autosave wrote to
                                save_path_str = st.session_state.get("save_path") or str(
                                    DEFAULT_DMP_PATH
                                )
                                save_path = Path(save_path_str).resolve()

                                # 4) Run dataset_path_update on the saved DMP
                                try:
                                    dataset_path_update(
                                        data_files=chosen_norm,
                                        dmp_path=save_path,
                                        git_msg=(
                                            f"Updating dataset #{i + 1} data path to {chosen_norm}"
                                        ),
                                    )
                                except Exception as e:
                                    st.warning(f"dataset_path_update failed: {e}")

                                # 5) Reload DMP + force widget refresh + reset UI / autosave baseline, then rerun
                                _reload_dmp_from_disk(
                                    save_path,
                                    clear_widget_keys=True,
                                    reset_autosave_baseline=True,
                                    force_widget_refresh=True,
                                    rerun=True,
                                )

                            except Exception as e:
                                st.error(f"Failed to save DMP after updating data path: {e}")

            # If we reached here, the dataset wasn't deleted in this run
            datasets[i] = edit_any(ds, path=("dmp", "dataset", i), ns=f"deep_v{widget_version}")

            new_is_reused = _is_reused(datasets[i])
            if prev_is_reused != new_is_reused:
                is_reused_changed = True

            changed = False
            changed |= _enforce_personal_implies_sensitive(datasets[i])
            changed |= _enforce_restricted_sensitive_lock(datasets[i])
            changed |= _enforce_privacy_access(datasets[i])
            changed |= _normalize_license_by_access(datasets[i])
            changed |= _ensure_open_has_license(datasets[i])
            changed |= _refresh_blurred_data_files_for_sensitive(datasets[i])
            changed |= _refresh_unblurred_data_files_for_non_sensitive(datasets[i])

    # Rerun if is_reused changed to update button states
    if is_reused_changed:
        st.rerun()

    _sync_sensitive_policy_artifacts(datasets)


def _is_reused(ds: dict) -> bool:
    v = ds.get("is_reused")
    return str(v).strip().lower() in {"true", "1", "yes"}


TOKENS_STATE = {
    "zenodo": {"env_key": "ZENODO_TOKEN", "state_key": "__token_zenodo__"},
    "dataverse": {"env_key": "DATAVERSE_TOKEN", "state_key": "__token_dataverse__"},
}


# ---------------------------
# Helpers to read secrets/env with fallback
# ---------------------------
def _get_env_or_secret(key: str, default: str = "") -> str:
    val = ""
    try:
        val = st.secrets[key]  # type: ignore[index]
    except Exception:
        val = ""
    if not val:
        val = load_from_env(key) or ""
    return val or default


# ---------------------------
# University→Dataverse helpers
# ---------------------------
def _extract_domain_candidates_from_context(dmp_data: dict[str, Any] | None) -> list[str]:
    candidates: list[str] = []
    # DMP root contact (minimal inference used in your latest version)
    try:
        mbox = (dmp_data or {}).get("dmp", {}).get("contact", {}).get("mbox", "")
        if isinstance(mbox, str) and "@" in mbox:
            candidates.append(mbox.split("@", 1)[1].lower())
    except Exception:
        pass

    # Normalize against DK_UNI_MAP keys
    normalized: list[str] = []
    for dom in candidates:
        for key in DK_UNI_MAP.keys():
            if dom == key or dom.endswith("." + key):
                normalized.append(key)
                break
        else:
            normalized.append(dom)
    uniq: list[str] = []
    for d in normalized:
        if d not in uniq:
            uniq.append(d)
    return uniq


def _guess_dataverse_defaults_from_university(
    dmp_data: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    for dom in _extract_domain_candidates_from_context(dmp_data):
        info = DK_UNI_MAP.get(dom)
        if info:
            return info.get("dataverse_default_base_url"), info.get("dataverse_alias")
    return None, None


def _safe_get_json(
    url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 8
):
    try:
        r = requests.get(url, params=params or {}, headers=headers or {}, timeout=timeout)
        ctype = r.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return r.status_code, r.json()
        return r.status_code, r.text
    except requests.exceptions.RequestException as e:
        return None, str(e)


def test_zenodo_connection(
    api_base: str, token: str, community: str | None = None
) -> tuple[bool, str]:
    """Check Zenodo reachability, optional token validity, and optional community existence."""
    if not api_base:
        return False, "No Zenodo API base URL configured."

    # 1) Base reachability (public)
    status, _ = _safe_get_json(api_base.rstrip("/") + "/records", timeout=6)
    if status != 200:
        return False, f"Cannot reach Zenodo at {api_base} (HTTP {status})."

    # 2) Token check (optional)
    if token:
        status, body = _safe_get_json(
            api_base.rstrip("/") + "/deposit/depositions", params={"access_token": token}, timeout=8
        )
        if status == 200:
            token_ok = True
        elif status in (401, 403):
            return False, "Zenodo reachable, but the token was rejected (401/403)."
        else:
            return False, f"Zenodo token check returned HTTP {status}: {body}"
    else:
        token_ok = False  # no token provided

    # 3) Community check (optional)
    community = (community or "").strip()
    if community:
        # Try direct lookup first
        status_c, body_c = _safe_get_json(
            api_base.rstrip("/") + f"/communities/{community}", timeout=8
        )
        found = False
        if status_c == 200:
            # In Zenodo JSON, the slug is typically in 'id' or 'slug'
            if isinstance(body_c, dict):
                slug_match = (
                    str(body_c.get("id") or body_c.get("slug") or "").lower() == community.lower()
                )
                found = slug_match or True  # treat 200 as found even if fields differ
        elif status_c == 404:
            # Fallback: search endpoint (older deployments)
            status_s, body_s = _safe_get_json(
                api_base.rstrip("/") + "/communities", params={"q": community, "page": 1}, timeout=8
            )
            if status_s == 200 and isinstance(body_s, dict):
                # Two possible shapes: {"hits":{"hits":[...]}} or {"hits":[...]}
                hits = body_s.get("hits", {})
                if isinstance(hits, dict):
                    items = hits.get("hits", [])
                else:
                    items = hits
                for it in items or []:
                    cand = str((it or {}).get("id") or (it or {}).get("slug") or "")
                    if cand.lower() == community.lower():
                        found = True
                        break
        elif status_c in (401, 403):
            return (
                False,
                f"Zenodo reachable, but access denied when checking community '{community}' (HTTP {status_c}).",
            )
        else:
            return False, f"Community check returned HTTP {status_c}."

        if not found:
            return (
                False,
                f"Zenodo reachable{', token OK' if token_ok else ''}, but community '{community}' was not found.",
            )

        # Community exists
        if token_ok:
            return True, f"Zenodo reachable ✅, token OK, and community '{community}' found."
        return True, f"Zenodo reachable ✅ and community '{community}' found (no token check)."

    # No community provided
    if token_ok:
        return True, "Zenodo reachable ✅ and token looks valid."
    return True, "Zenodo reachable ✅ (no token provided, skipped token check)."


def test_dataverse_connection(base_url: str, token: str, alias: str) -> tuple[bool, str]:
    if not base_url:
        return False, "No Dataverse base URL configured."
    # Base reachability
    status, body = _safe_get_json(base_url.rstrip("/") + "/api/info/version", timeout=6)
    if status != 200:
        return False, f"Cannot reach Dataverse at {base_url} (HTTP {status})."

    # Token check (optional)
    if token:
        status_me, _ = _safe_get_json(
            base_url.rstrip("/") + "/api/users/:me", params={"key": token}, timeout=8
        )
        if status_me != 200:
            if status_me in (401, 403):
                return False, "Dataverse reachable, but the API token was rejected (401/403)."
            return False, f"Dataverse '/users/:me' returned HTTP {status_me}."

    # Alias check (optional but useful)
    if alias:
        status_alias, _ = _safe_get_json(
            base_url.rstrip("/") + f"/api/dataverses/{alias}",
            params={"key": token} if token else None,
            timeout=8,
        )
        if status_alias == 200:
            if token:
                return True, "Dataverse reachable ✅, token OK, and collection (alias) found."
            return True, "Dataverse reachable ✅ and collection (alias) found (no token check)."
        elif status_alias == 404:
            return (
                False,
                f"Dataverse reachable, but collection (alias '{alias}') was not found (404).",
            )
        elif status_alias in (401, 403):
            return False, "Dataverse reachable, alias provided, but access denied (401/403)."
        else:
            return False, f"Dataverse alias check returned HTTP {status_alias}."

    # No alias provided
    if token:
        return True, "Dataverse reachable ✅ and token looks valid (no alias provided)."
    return True, "Dataverse reachable ✅ (no token or alias provided)."


def get_zenodo_community() -> str:
    return (
        st.session_state.get("zenodo_community") or _get_env_or_secret("ZENODO_COMMUNITY", "")
    ).strip()


# ---------------------------
# Sidebar controls (tokens + sites)
# ---------------------------
def render_token_controls():
    with st.sidebar:
        st.header("Repositories & tokens")

        # --------------- Zenodo ---------------
        st.subheader("Zenodo")

        z_api_default = _get_env_or_secret("ZENODO_API_BASE", "https://sandbox.zenodo.org/api")
        if "zenodo_api_base" not in st.session_state:
            st.session_state["zenodo_api_base"] = z_api_default

        z_comm_default = _get_env_or_secret("ZENODO_COMMUNITY", "")
        if "zenodo_community" not in st.session_state:
            st.session_state["zenodo_community"] = z_comm_default

        z_token_default = _get_env_or_secret(TOKENS_STATE["zenodo"]["env_key"], "")
        if TOKENS_STATE["zenodo"]["state_key"] not in st.session_state:
            st.session_state[TOKENS_STATE["zenodo"]["state_key"]] = z_token_default

        zenodo_options = [u for (u, _label) in ZENODO_API_CHOICES]
        try:
            z_index = zenodo_options.index(st.session_state["zenodo_api_base"])
        except ValueError:
            z_index = 0

        with st.form("zenodo_settings_form", clear_on_submit=False):
            z_api = st.selectbox(
                "Site",
                options=zenodo_options,
                index=z_index,
                format_func=lambda u: dict(ZENODO_API_CHOICES)[u],
                help="Sandbox is highly recommended for testing.",
            )
            z_comm = st.text_input(
                "Community (optional)",
                value=st.session_state["zenodo_community"],
                placeholder="e.g. cbs, ku, sdu…",
                help="Community identifier (slug). Leave blank to omit.",
                key="__zen_comm__",
            )
            z_token = st.text_input(
                "API token",
                type="password",
                value=st.session_state[TOKENS_STATE["zenodo"]["state_key"]],
                help="Click Save to write to .env and update session.",
                key="__zen_token__",
            )
            z_submit = st.form_submit_button("Save settings")
            if z_submit:
                save_to_env(z_api, "ZENODO_API_BASE")
                os.environ["ZENODO_API_BASE"] = z_api
                st.session_state["zenodo_api_base"] = z_api

                if z_token.strip():
                    save_to_env(z_token.strip(), TOKENS_STATE["zenodo"]["env_key"])
                    os.environ[TOKENS_STATE["zenodo"]["env_key"]] = z_token.strip()
                    st.session_state[TOKENS_STATE["zenodo"]["state_key"]] = z_token.strip()

                st.session_state["zenodo_community"] = z_comm.strip()
                if z_comm.strip():
                    save_to_env(z_comm.strip(), "ZENODO_COMMUNITY")
                    os.environ["ZENODO_COMMUNITY"] = z_comm.strip()

                # Run connection test using effective values in session
                eff_api = st.session_state.get("zenodo_api_base", z_api)
                eff_token = st.session_state.get(
                    TOKENS_STATE["zenodo"]["state_key"], z_token.strip()
                )
                eff_comm = (st.session_state.get("zenodo_community") or "").strip()

                ok, msg = test_zenodo_connection(eff_api, eff_token, eff_comm)
                (st.success if ok else st.error)(msg)

        # --------------- Dataverse ---------------
        st.subheader("Dataverse")

        dv_base_default = _get_env_or_secret("DATAVERSE_BASE_URL", "")
        dv_alias_default = _get_env_or_secret("DATAVERSE_ALIAS", "")

        if not dv_base_default or not dv_alias_default:
            guess_base, guess_alias = _guess_dataverse_defaults_from_university(
                st.session_state.get("data")
            )
            if not dv_base_default:
                dv_base_default = guess_base or "https://demo.dataverse.deic.dk"
            if not dv_alias_default:
                dv_alias_default = guess_alias or ""

        dv_token_default = _get_env_or_secret(TOKENS_STATE["dataverse"]["env_key"], "")

        if "dataverse_base_url" not in st.session_state:
            st.session_state["dataverse_base_url"] = dv_base_default
        if "dataverse_site_choice" not in st.session_state:
            if "demo.dataverse.deic.dk" in dv_base_default:
                st.session_state["dataverse_site_choice"] = "https://demo.dataverse.deic.dk"
            elif "dataverse.deic.dk" in dv_base_default:
                st.session_state["dataverse_site_choice"] = "https://dataverse.deic.dk"
            else:
                st.session_state["dataverse_site_choice"] = "other"
        if "dataverse_alias" not in st.session_state:
            st.session_state["dataverse_alias"] = dv_alias_default
        if TOKENS_STATE["dataverse"]["state_key"] not in st.session_state:
            st.session_state[TOKENS_STATE["dataverse"]["state_key"]] = dv_token_default

        dv_options = [v for (v, _label) in DATAVERSE_SITE_CHOICES]
        try:
            dv_idx = dv_options.index(st.session_state["dataverse_site_choice"])
        except ValueError:
            dv_idx = 0

        with st.form("dataverse_settings_form", clear_on_submit=False):
            dv_choice = st.selectbox(
                "Site",
                options=dv_options,
                index=dv_idx,
                format_func=lambda v: dict(DATAVERSE_SITE_CHOICES)[v],
                help="Pick demo or production. Choose 'Other…' to enter a custom base URL.",
                key="__dv_site__",
            )

            # base URL input (for 'other') or fixed (DeiC)
            if dv_choice == "other":
                custom_url = st.text_input(
                    "Custom Dataverse base URL",
                    value=st.session_state["dataverse_base_url"]
                    if st.session_state["dataverse_site_choice"] == "other"
                    else "",
                    placeholder="https://your.dataverse.org",
                    key="__dv_custom_base__",
                )
                dv_base = custom_url.strip()
            else:
                dv_base = dv_choice

            # Prefill alias if we can guess it
            guess_alias_for_ui = ""
            if dv_choice != "other":
                _gb_ui, _ga_ui = _guess_dataverse_defaults_from_university(
                    st.session_state.get("data")
                )
                guess_alias_for_ui = _ga_ui or ""

            if dv_choice != "other":
                if not st.session_state.get("__dv_alias_input__") and not st.session_state.get(
                    "dataverse_alias"
                ):
                    if guess_alias_for_ui:
                        st.session_state["__dv_alias_input__"] = guess_alias_for_ui

            dv_alias = st.text_input(
                "Collection (alias)",
                value=st.session_state.get(
                    "__dv_alias_input__",
                    st.session_state.get("dataverse_alias", "") or guess_alias_for_ui or "",
                ),
                placeholder=(guess_alias_for_ui or "e.g. your-collection-alias"),
                help="Alias (URL-friendly identifier) of the target Dataverse collection.",
                key="__dv_alias_input__",
            )

            dv_token = st.text_input(
                "API token",
                type="password",
                value=st.session_state[TOKENS_STATE["dataverse"]["state_key"]],
                help="Click Save to write to .env and update session.",
                key="__dv_token__",
            )

            dv_submit = st.form_submit_button("Save settings")
            if dv_submit:
                alias_to_save = st.session_state.get("__dv_alias_input__", "").strip()
                if dv_choice != "other" and alias_to_save == "":
                    _gb, guess_alias = _guess_dataverse_defaults_from_university(
                        st.session_state.get("data")
                    )
                    if guess_alias:
                        alias_to_save = guess_alias

                if dv_choice == "other" and not dv_base:
                    st.warning("Please enter a custom Dataverse base URL.")
                else:
                    st.session_state["dataverse_site_choice"] = dv_choice
                    st.session_state["dataverse_base_url"] = dv_base
                    save_to_env(dv_base, "DATAVERSE_BASE_URL")
                    os.environ["DATAVERSE_BASE_URL"] = dv_base

                    st.session_state["dataverse_alias"] = alias_to_save
                    save_to_env(alias_to_save, "DATAVERSE_ALIAS")

                    if dv_token.strip():
                        save_to_env(dv_token.strip(), TOKENS_STATE["dataverse"]["env_key"])
                        os.environ[TOKENS_STATE["dataverse"]["env_key"]] = dv_token.strip()
                        st.session_state[TOKENS_STATE["dataverse"]["state_key"]] = dv_token.strip()

                    # Run connection test using effective values in session
                    eff_base = st.session_state.get("dataverse_base_url", dv_base)
                    eff_token = st.session_state.get(
                        TOKENS_STATE["dataverse"]["state_key"], dv_token.strip()
                    )
                    eff_alias = st.session_state.get("dataverse_alias", alias_to_save)
                    ok, msg = test_dataverse_connection(eff_base, eff_token, eff_alias)
                    (st.success if ok else st.error)(msg)


# ---------------------------
# Helpers to retrieve chosen endpoints
# ---------------------------


def get_token_from_state(service: str) -> str:
    """Read the best-known token without rendering UI."""
    service = service.lower().strip()
    env_key = TOKENS_STATE[service]["env_key"]
    state_key = TOKENS_STATE[service]["state_key"]

    val = st.session_state.get(state_key, "")
    if val:
        return val
    try:
        val = st.secrets[env_key]  # type: ignore[index]
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(env_key) or (load_from_env(env_key) or "")


def get_zenodo_api_base() -> str:
    return st.session_state.get("zenodo_api_base") or _get_env_or_secret(
        "ZENODO_API_BASE", "https://sandbox.zenodo.org/api"
    )


def get_dataverse_config() -> tuple[str, str]:
    base = st.session_state.get("dataverse_base_url") or _get_env_or_secret(
        "DATAVERSE_BASE_URL", ""
    )
    alias = st.session_state.get("dataverse_alias") or _get_env_or_secret("DATAVERSE_ALIAS", "")

    if not base or not alias:
        guess_base, guess_alias = _guess_dataverse_defaults_from_university(
            st.session_state.get("data")
        )
        base = base or guess_base or "https://demo.dataverse.deic.dk"
        alias = alias or guess_alias or ""
    return base, alias


# ──────────────────────────────────────────────────────────────────────────────
# Autosave helpers
# ──────────────────────────────────────────────────────────────────────────────


def _ordered_output_without_touching_modified() -> dict[str, Any]:
    """
    Make a canonical, ordered snapshot WITHOUT bumping dmp.modified.
    Used only for autosave-change detection.
    """
    snap = deepcopy(st.session_state["data"])
    # Apply fixups and order on the copy
    snap = reorder_dmp_keys(_schema_fixups_in_place(snap))
    # Ensure key exists but don't change it
    snap.setdefault("dmp", {}).setdefault("modified", snap.get("dmp", {}).get("modified", ""))
    return snap


def _json_hash_for_autosave(obj: dict[str, Any]) -> str:
    """
    Stable hash for change detection (excluding runtime timestamp noise).
    """
    # sort_keys True + compact separators gives deterministic string
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _autosave_if_changed(force_write: bool = False) -> None:
    """
    If the DMP content changed since last snapshot, bump modified and write to disk.
    Also sync cookiecutter.json from the current DMP.

    If `force_write=True`, we always write once even if this is the first call
    (i.e. no existing baseline hash yet).
    """
    if "save_path" not in st.session_state or not st.session_state["save_path"]:
        return
    base_path = Path(st.session_state["save_path"]).resolve()
    base_path.parent.mkdir(parents=True, exist_ok=True)

    # Compute current content snapshot (without touching modified)
    current_snapshot = _ordered_output_without_touching_modified()
    current_hash = _json_hash_for_autosave(current_snapshot)

    first_call = "__autosave_last_hash__" not in st.session_state

    # First call: normally just seed baseline (no write),
    # but if force_write=True we fall through and write immediately.
    if first_call and not force_write:
        st.session_state["__autosave_last_hash__"] = current_hash
        st.session_state["__autosave_feedback__"] = (
            f"Autosave ready – changes will be saved to {base_path.name}"
        )
        return

    # If not first call and hash unchanged, nothing to do
    if (not first_call) and current_hash == st.session_state["__autosave_last_hash__"]:
        return

    # Something changed (or we explicitly forced a write) → bump modified and save
    to_save = deepcopy(current_snapshot)
    try:
        to_save["dmp"]["modified"] = now_iso_minute()
    except Exception:
        pass

    try:
        base_path.write_text(
            json.dumps(to_save, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        st.session_state["__autosave_last_hash__"] = current_hash
        st.session_state["__autosave_feedback__"] = (
            f"💾 Autosaved {base_path.name} at {datetime.now().strftime('%H:%M:%S')}"
        )

        # Keep cookiecutter.json in sync with the autosaved DMP
        try:
            update_cookiecutter_from_dmp(dmp_path=base_path)
        except Exception as e:
            st.session_state["__autosave_feedback__"] += f" (cookiecutter sync failed: {e})"

    except Exception as e:
        st.session_state["__autosave_feedback__"] = f"⚠️ Autosave failed: {e}"


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────


def _schema_fixups_in_place(data: dict[str, Any]) -> dict[str, Any]:
    schema = safe_fetch_schema()
    normalize_root_in_place(data, schema=schema)
    normalize_datasets_in_place(data, schema=schema)
    if schema:
        ensure_required_by_schema(data, schema)
        repair_empty_enums(
            data.get("dmp", {}),
            schema,
            schema.get("properties", {}).get("dmp", {}),
            path="dmp",
        )
    return data


def _ensure_data_initialized(default_path: Path) -> None:
    st.session_state.setdefault("__loaded_from__", "")
    st.session_state.setdefault("__last_upload_hash__", None)
    st.session_state.setdefault("__load_message__", "")
    st.session_state.setdefault("save_path", str(default_path))

    if "data" in st.session_state and st.session_state["data"]:
        st.session_state["data"] = ensure_dmp_shape(st.session_state["data"])
        return

    if default_path.exists():
        try:
            with default_path.open("r", encoding="utf-8") as f:
                st.session_state["data"] = ensure_dmp_shape(json.load(f))
            st.session_state["__loaded_from__"] = str(default_path.resolve())
            st.session_state["__load_message__"] = (
                f"✅ Loaded default DMP from {default_path.resolve()}"
            )
            st.session_state["save_path"] = str(default_path.resolve())
            return
        except Exception as e:
            st.warning(f"Failed to load default DMP. Started empty. Error: {e}")

    st.session_state["data"] = ensure_dmp_shape({})
    st.session_state["__loaded_from__"] = "new"
    if not str(st.session_state.get("__load_message__", "")).strip():
        st.session_state["__load_message__"] = "⚠️ Started with an empty DMP."
    st.session_state["save_path"] = str(default_path)


def _bootstrap_dmp_from_selected_parent(default_path: Path) -> bool:
    """
    Standalone bootstrap: if no dmp.json exists, prompt for dataset parent folder,
    persist [tool.datasets].patterns, and initialize DMP via dataset_main
    with sub_dir=False.
    """
    if default_path.exists():
        return False

    if st.session_state.get("__bootstrap_no_dmp_done__", False):
        return False

    st.session_state["__bootstrap_no_dmp_done__"] = True

    chosen = _browse_for_directory(
        start_path=str(PROJECT_ROOT),
        title="Select parent data folder for initial DMP dataset scan",
        dir_only=True,
    )
    if not chosen:
        st.session_state["__load_message__"] = (
            "No dmp.json found and initialization was skipped (folder selection canceled). "
            "Choose a parent data path to initialize datasets, or create/load a DMP manually."
        )
        return False

    chosen_norm = _normalize_chosen_path(chosen)
    default_dataset_path = {"parent_path": chosen_norm, "sub_dir": False}

    write_toml(
        data={"patterns": [chosen_norm]},
        folder=str(PROJECT_ROOT),
        json_filename=JSON_FILENAME,
        tool_name="datasets",
        toml_path=TOML_PATH,
    )

    try:
        dataset_main(
            dmp_path=str(default_path),
            do_print=False,
            git_msg="Initialize DMP from selected parent data path",
            default_dataset_path=default_dataset_path,
        )
        st.session_state["__load_message__"] = (
            f"Initialized DMP from parent data path: {chosen_norm}"
        )
        return True
    except Exception as e:
        st.warning(f"Failed initial dataset bootstrap: {e}")
        return False


def _resolve_data_parent_path() -> Path:
    datasets_cfg = (
        read_toml(
            folder=str(PROJECT_ROOT),
            json_filename=JSON_FILENAME,
            tool_name="datasets",
            toml_path=TOML_PATH,
        )
        or {}
    )
    patterns = datasets_cfg.get("patterns", [])
    first_pattern = ""
    if isinstance(patterns, str):
        first_pattern = patterns.strip()
    elif isinstance(patterns, list):
        for p in patterns:
            if isinstance(p, str) and p.strip():
                first_pattern = p.strip()
                break

    if not first_pattern:
        return PROJECT_ROOT

    cleaned = first_pattern.replace("\\", "/")
    if cleaned.endswith("/*"):
        cleaned = cleaned[:-2]
    cleaned = cleaned.rstrip("/")
    path_candidate = Path(cleaned or ".")
    if not path_candidate.is_absolute():
        path_candidate = (PROJECT_ROOT / path_candidate).resolve()
    return path_candidate


def main() -> None:
    launch_root_raw = os.environ.get("REPOKIT_DMP_PROJECT_ROOT", "").strip()
    launch_root = Path(launch_root_raw).resolve() if launch_root_raw else Path.cwd().resolve()
    ensure_project_root(launch_root)
    os.chdir(str(launch_root))
    # Refresh runtime globals after Streamlit process bootstrap.
    import repokit_common as _repokit_common

    global PROJECT_ROOT, DATA_PARENT_PATH
    PROJECT_ROOT = _repokit_common.PROJECT_ROOT
    default_path = find_default_dmp_path()
    _bootstrap_dmp_from_selected_parent(default_path)
    DATA_PARENT_PATH = _resolve_data_parent_path()

    st.set_page_config(page_title=f"RDA-DMP {SCHEMA_VERSION} JSON Editor", layout="wide")
    st.title(f"RDA-DMP {SCHEMA_VERSION} JSON Editor")

    # Small status line for autosave
    if st.session_state.get("__autosave_feedback__"):
        st.caption(st.session_state["__autosave_feedback__"])

    _inject_dist_css_once()

    # Session defaults (for UX)
    st.session_state.setdefault("__loaded_from__", "")
    st.session_state.setdefault("__load_message__", "")
    st.session_state.setdefault("__last_upload_hash__", None)
    st.session_state.setdefault("__uploader_ver__", 0)
    st.session_state.setdefault("save_path", "")

    # Load schema & default DMP path
    schema_now = safe_fetch_schema()

    # IMPORTANT: initialize data BEFORE rendering the sidebar
    _ensure_data_initialized(default_path)

    # Sidebar: Sites & Tokens (Zenodo / Dataverse)
    render_token_controls()

    # Show load message
    if st.session_state.get("__load_message__"):
        st.info(st.session_state["__load_message__"])

    # Helper: produce ordered/validated output snapshot (manual download/validate)
    def _current_ordered_output() -> dict[str, Any]:
        st.session_state["data"]["dmp"]["modified"] = now_iso_minute()
        return reorder_dmp_keys(_schema_fixups_in_place(deepcopy(st.session_state["data"])))

    # Working folder for all files = directory of the default dmp
    working_folder: Path = (
        Path(st.session_state["save_path"]).resolve().parent
        if st.session_state.get("save_path")
        else default_path.parent
    )

    # Sidebar: Load / Save
    with st.sidebar:
        st.header("Load / Save")
        st.caption(f"Schema: {'✅ loaded' if schema_now else '⚠️ unavailable (fallbacks)'}")

        uploader_key = f"open_json_uploader_{st.session_state['__uploader_ver__']}"
        uploaded = st.file_uploader(
            "Open JSON",
            type=["json"],
            help=f"Uploads are saved to: {working_folder.resolve()}",
            key=uploader_key,
        )

        if uploaded is not None:
            payload = uploaded.getvalue()
            h = hashlib.sha256(payload).hexdigest()
            if st.session_state.get("__last_upload_hash__") != h:
                try:
                    working_folder.mkdir(parents=True, exist_ok=True)
                    dst_path = (working_folder / uploaded.name).resolve()
                    dst_path.write_bytes(payload)

                    data = json.loads(payload.decode("utf-8"))
                    st.session_state["data"] = ensure_dmp_shape(data)
                    st.session_state["__last_upload_hash__"] = h
                    st.session_state["__loaded_from__"] = str(dst_path)
                    st.session_state["__load_message__"] = f"✅ Loaded DMP from {dst_path}"
                    st.session_state["save_path"] = str(dst_path)
                    st.session_state.pop("ds_selected", None)
                    # reset autosave baseline to newly loaded content
                    st.session_state.pop("__autosave_last_hash__", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to load uploaded JSON: {e}")

        elif st.session_state.get("__last_upload_hash__"):
            st.session_state["__last_upload_hash__"] = None
            try:
                if default_path.exists():
                    with default_path.open("r", encoding="utf-8") as f:
                        st.session_state["data"] = ensure_dmp_shape(json.load(f))
                    st.session_state["__loaded_from__"] = str(default_path.resolve())
                    st.session_state["__load_message__"] = (
                        f"✅ Loaded default DMP from {default_path.resolve()}"
                    )
                    st.session_state["save_path"] = str(default_path.resolve())
                else:
                    new_path = (working_folder / "new_dmp.json").resolve()
                    st.session_state["data"] = ensure_dmp_shape({})
                    st.session_state["__loaded_from__"] = "new"
                    st.session_state["__load_message__"] = (
                        f"⚠️ Default DMP not found. Started a new DMP (will save to {new_path})"
                    )
                    st.session_state["save_path"] = str(new_path)
                st.session_state.pop("ds_selected", None)
                st.session_state.pop("__autosave_last_hash__", None)
                st.rerun()
            except Exception as e:
                st.error(f"Failed to reload default DMP: {e}")

        if st.button("➕ Create New DMP"):
            st.session_state["data"] = ensure_dmp_shape({})
            new_path = (working_folder / "new_dmp.json").resolve()
            st.session_state["__loaded_from__"] = "new"
            st.session_state["__load_message__"] = f"✅ Started a new DMP (will save to {new_path})"
            st.session_state["save_path"] = str(new_path)
            st.session_state["__last_upload_hash__"] = None
            st.session_state.pop("ds_selected", None)
            st.session_state["__uploader_ver__"] += 1
            st.session_state.pop("__autosave_last_hash__", None)
            st.rerun()

        out_for_dl = _current_ordered_output()

        download_clicked = st.download_button(
            "⬇️ Download JSON",
            data=json.dumps(out_for_dl, indent=4, ensure_ascii=False).encode("utf-8"),
            file_name=Path(st.session_state["save_path"]).name or "dmp.json",
            mime="application/json",
            key="download",
        )

        if download_clicked:
            # When the user downloads, also sync cookiecutter.json from *this* DMP
            try:
                save_path = Path(st.session_state["save_path"]).resolve()
                # Ensure the on-disk DMP matches what was just downloaded
                save_path.write_text(
                    json.dumps(out_for_dl, indent=4, ensure_ascii=False),
                    encoding="utf-8",
                )
                update_cookiecutter_from_dmp(dmp_path=save_path)
                # Optional small hint in the UI (non-blocking)
                st.caption("✅ cookiecutter.json updated from downloaded DMP")
            except Exception as e:
                st.warning(f"cookiecutter.json could not be updated: {e}")

    # Main editor area
    data = ensure_dmp_shape(st.session_state["data"])
    dmp_root = data["dmp"]
    draw_root_section(dmp_root)
    draw_projects_section(dmp_root)
    draw_datasets_section(dmp_root)

    # ---- AUTOSAVE: run after all edits have been applied
    _autosave_if_changed()


def cli() -> None:
    launch_root = Path.cwd().resolve()
    os.environ["REPOKIT_DMP_PROJECT_ROOT"] = str(launch_root)
    ensure_project_root(launch_root)
    os.chdir(str(launch_root))
    app_path = Path(__file__).resolve()
    ssh_mode = len(sys.argv) > 1 and sys.argv[1] == "ssh"
    if ssh_mode:
        sys.argv.pop(1)
        app_port = int(os.environ.get("DMP_PORT", "8501"))
        cmd = f"ssh -N -L {app_port}:localhost:{app_port} example@host.dk -p PORT (The host and port you have connected)"
        print("\n=== SSH port forwarding ===")
        print("Run this on your LOCAL machine, then open the URL below:")
        print(f"  {cmd}\n")
        sys.argv = [
            "streamlit",
            "run",
            str(app_path),
            "--server.headless",
            "true",
            "--server.address",
            "localhost",
            "--server.port",
            str(app_port),
            *sys.argv[1:],
        ]
    else:
        sys.argv = ["streamlit", "run", str(app_path), *sys.argv[1:]]
    sys.exit(st_main())


if __name__ == "__main__":
    main()
