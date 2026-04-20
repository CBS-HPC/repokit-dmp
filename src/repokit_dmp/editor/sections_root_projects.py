from copy import deepcopy
from pathlib import Path
from typing import Any

import requests
import streamlit as st

from .bootstrap_and_policies import (
    DATA_PARENT_PATH,
    DEFAULT_DMP_PATH,
    DK_UNI_MAP,
    JSON_FILENAME,
    PROJECT_ROOT,
    TOML_PATH,
    PublishError,
    dataset_main,
    dataset_path_update,
    dmp_default_templates,
    get_repokit_info_payload,
    load_from_env,
    streamlit_publish_to_dataverse,
    streamlit_publish_to_zenodo,
    write_toml,
    _enforce_personal_implies_sensitive,
    _enforce_restricted_sensitive_lock,
    _refresh_blurred_data_files_for_sensitive,
    _refresh_unblurred_data_files_for_non_sensitive,
    _sync_sensitive_policy_artifacts,
)
from .schema_and_editors import (
    _enforce_privacy_access,
    _is_empty_alias,
    _key_for,
    _normalize_license_by_access,
    _normalize_chosen_path,
    _ensure_open_has_license,
)
from .widget_helpers import (
    _browse_for_directory,
    _reload_dmp_from_disk,
    _resolve_browse_start_path,
    edit_any,
)


def _dataset_heading_label(ds: dict, index: int, title: str) -> str:
    info = get_repokit_info_payload(ds) or {}
    number_of_files = info.get("number_of_files")
    data_files = info.get("data_files")
    is_scanned = bool(info)
    is_empty = is_scanned and (
        number_of_files == 0 or (isinstance(data_files, list) and len(data_files) == 0)
    )
    suffix = " (Empty)" if is_empty else ""
    return f"Dataset #{index + 1}: {title}{suffix}"


def draw_datasets_section(dmp_root: dict) -> None:
    # Local import avoids module-level circular dependency with sections_datasets.
    from .sections_datasets import (
        _autosave_if_changed,
        get_dataverse_config,
        get_token_from_state,
        get_zenodo_api_base,
        get_zenodo_community,
    )

    st.subheader("Datasets")

    # Get widget refresh counter for forcing widget recreation
    widget_version = st.session_state.get("__widget_refresh_counter__", 0)

    datasets = dmp_root.get("dataset")
    if not isinstance(datasets, list):
        datasets = []
        dmp_root["dataset"] = datasets

    # --- Top row: Add dataset + Parent Data Path + Change Path + Scan Path ---
    top = st.columns([1, 4, 1, 1])
    templates = dmp_default_templates()

    # Initialize parent data path in session (for later override)
    if "__parent_data_path__" not in st.session_state:
        st.session_state["__parent_data_path__"] = str(DATA_PARENT_PATH)

    parent_data_path = st.session_state["__parent_data_path__"]

    def _apply_parent_data_path(source_path: str, action_label: str) -> None:
        resolved_parent = _resolve_browse_start_path(source_path, fallback_root=PROJECT_ROOT)
        chosen_norm = _normalize_chosen_path(str(resolved_parent))

        # Keep the UI state in sync with the active scan target.
        st.session_state["__parent_data_path__"] = chosen_norm

        try:
            # 1) Let the central autosave logic write the DMP to disk
            _autosave_if_changed(force_write=True)

            # Resolve the path that autosave wrote to
            save_path_str = st.session_state.get("save_path") or str(DEFAULT_DMP_PATH)
            save_path = Path(save_path_str).resolve()

            # 2) Persist to TOML
            write_toml(
                data={"patterns": [chosen_norm]},
                folder=str(PROJECT_ROOT),
                json_filename=JSON_FILENAME,
                tool_name="datasets",
                toml_path=TOML_PATH,
            )

            # 3) Rebuild dataset metadata
            try:
                dataset_main(
                    dmp_path=save_path,
                    do_print=False,
                    git_msg=action_label.format(path=chosen_norm),
                )
            except Exception as e:
                st.warning(f"dataset_main failed: {e}")

            # 4) Reload DMP + force widget refresh + reset UI / autosave baseline, then rerun
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
                _apply_parent_data_path(chosen, "Setting parent dataset path to {path}")

    with top[3]:
        if st.button("Scan Path", key="scan_parent_data_path"):
            _apply_parent_data_path(
                parent_data_path or st.session_state.get(
                    "__parent_data_path__", str(DATA_PARENT_PATH)
                ),
                "Scanning parent dataset path {path}",
            )

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
        header_label = _dataset_heading_label(ds, i, header_title)

        with st.expander(header_label, expanded=False):
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
