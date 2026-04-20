from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import time
from typing import Any

import streamlit as st

from .bootstrap_and_policies import (
    EXTRA_ENUMS,
    LICENSE_LINKS,
    PROJECT_ROOT,
    dmp_default_templates,
    fetch_schema,
    today_iso,
    _has_privacy_flags,
)


def _edit_any(value: Any, path: tuple, ns: str | None = None) -> Any:
    from .widget_helpers import edit_any

    return edit_any(value, path=path, ns=ns)


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
    # Local import avoids a module-level cycle with widget_helpers.
    from .widget_helpers import edit_any

    if not isinstance(arr, list):
        return arr
    if not arr:
        templates = dmp_default_templates()
        arr.append(deepcopy(templates["distribution"]))
    if len(arr) == 1 and isinstance(arr[0], dict):
        _inject_dist_css_once()
        with st.expander("Distribution", expanded=True):
            arr[0] = _edit_any(arr[0], path=(*path, 0), ns=ns)
        return arr
    return edit_array(arr, path=path, title_singular="Distribution", removable_items=True, ns=ns)


def _edit_dataset_id_inline(obj: Any, path: tuple, ns: str | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict) or not obj:
        templates = dmp_default_templates()
        obj = deepcopy(templates["dataset"]["dataset_id"])
    with st.expander("Dataset ID", expanded=True):
        obj = _edit_any(obj, path=path, ns=ns)
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
    fail_key = "__rda_schema_failed_at__"
    cached = st.session_state.get(key, ...)
    if cached not in (..., None):
        return cached

    failed_at = st.session_state.get(fail_key)
    if isinstance(failed_at, (int, float)) and (time.monotonic() - failed_at) < 60:
        return None

    try:
        sch = fetch_schema()
        st.session_state[key] = sch
        st.session_state.pop(fail_key, None)
        return sch
    except Exception as exc:
        st.session_state[key] = None
        st.session_state[fail_key] = time.monotonic()
        st.session_state["__rda_schema_error__"] = str(exc)
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
        arr[0] = _edit_any(arr[0], path=(*path, 0), ns=ns)
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
                arr[i] = _edit_any(item, path=(*path, i), ns=ns)

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
