import json
import os
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import streamlit as st

from .bootstrap_and_policies import (
    DATAVERSE_SITE_CHOICES,
    ZENODO_API_CHOICES,
    ensure_dmp_shape,
    ensure_required_by_schema,
    load_from_env,
    normalize_datasets_in_place,
    normalize_root_in_place,
    now_iso_minute,
    reorder_dmp_keys,
    repair_empty_enums,
    save_to_env,
    update_cookiecutter_from_dmp,
)
from .schema_and_editors import safe_fetch_schema
from .sections_root_projects import (
    DK_UNI_MAP,
    TOKENS_STATE,
    _get_env_or_secret,
    _guess_dataverse_defaults_from_university,
    _safe_get_json,
)

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
        except Exception:
            # Do not surface cookiecutter sync failures in the autosave status line.
            pass

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
