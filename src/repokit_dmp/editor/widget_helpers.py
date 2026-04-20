import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import streamlit as st

from .bootstrap_and_policies import (
    PROJECT_ROOT,
    SCHEMA_URLS,
    SCHEMA_VERSION,
    dmp_default_templates,
    ensure_dmp_shape,
    wx,
    _is_restricted_dataset,
)
from .schema_and_editors import (
    _edit_dataset_id_inline,
    _edit_distribution_inline,
    _ensure_dataset_id_before_distribution,
    _enum_info_for_path,
    _enum_label_for,
    _is_dataset_path,
    _is_under_dataset_extension,
    _key_for,
    _normalize_chosen_path,  # noqa: F401 - re-export for app_main_cli compatibility
    edit_array,
    edit_primitive,
    _show_readonly_json,
)

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
    root = Path(start).resolve() if start is not None else PROJECT_ROOT
    return (root / "dmp.json").resolve()


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


def _resolve_browse_start_path(
    start_path: str | Path | None = None,
    fallback_root: Path | None = None,
) -> Path:
    """
    Resolve the initial chooser location.

    If the requested start path exists, use it.
    If it does not exist, fall back to the active project root.
    """
    root = (fallback_root or PROJECT_ROOT).resolve()

    if start_path:
        candidate = Path(os.fspath(start_path)).expanduser()
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve()
        else:
            try:
                candidate = candidate.resolve()
            except Exception:
                pass
        if candidate.exists():
            return candidate

    return root if root.exists() else Path.cwd().resolve()


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
    start = _resolve_browse_start_path(start_path)
    start_str = os.fspath(start)

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
