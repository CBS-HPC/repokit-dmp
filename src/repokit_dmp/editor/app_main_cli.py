import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.web.cli import main as st_main

from repokit_dmp.editor.bootstrap_and_policies import (
    DATA_PARENT_PATH,
    DEFAULT_DMP_PATH,
    JSON_FILENAME,
    PROJECT_ROOT,
    SCHEMA_VERSION,
    TOML_PATH,
    bootstrap_runtime_root,
    dataset_main,
    ensure_dmp_shape,
    load_from_env,
    now_iso_minute,
    read_toml,
    reorder_dmp_keys,
    save_to_env,
    update_cookiecutter_from_dmp,
    write_toml,
)
from repokit_dmp.editor.schema_and_editors import (
    _inject_dist_css_once,
    safe_fetch_schema,
)
from repokit_dmp.editor.sections_datasets import (
    _autosave_if_changed,
    _ensure_data_initialized,
    _schema_fixups_in_place,
    render_token_controls,
)
from repokit_dmp.editor.sections_root_projects import draw_datasets_section
from repokit_dmp.editor.widget_helpers import (
    _browse_for_directory,
    _normalize_chosen_path,
    draw_projects_section,
    draw_root_section,
    find_default_dmp_path,
)

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
            dmp_path=default_path,
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


def _ensure_data_policy_config() -> None:
    """
    Ensure pyproject.toml has [tool.data_policy] with default description
    and an explicit patterns list. If section is missing, create it as empty.
    """
    cfg = (
        read_toml(
            folder=str(PROJECT_ROOT),
            json_filename=JSON_FILENAME,
            tool_name="data_policy",
            toml_path=TOML_PATH,
        )
        or {}
    )

    payload = {
        "tool-description": cfg.get("tool-description")
        or (
            "Agent data-access policy: paths with sensitive/proprietary data "
            "that must be handled with restricted access and synced to agent ignore files."
        ),
        "patterns": cfg.get("patterns", []),
    }

    # Keep explicit empty list for first-time standalone generation.
    if not isinstance(payload["patterns"], list):
        payload["patterns"] = []

    write_toml(
        data=payload,
        folder=str(PROJECT_ROOT),
        json_filename=JSON_FILENAME,
        tool_name="data_policy",
        toml_path=TOML_PATH,
    )


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
    global PROJECT_ROOT, DATA_PARENT_PATH
    PROJECT_ROOT = bootstrap_runtime_root()
    _ensure_data_policy_config()
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
    bootstrap_runtime_root(launch_root)
    app_path = Path(__file__).resolve()
    ssh_mode = len(sys.argv) > 1 and sys.argv[1] == "ssh"
    if ssh_mode:
        sys.argv.pop(1)
        default_app_port = (
            load_from_env("APP_PORT")
            or os.environ.get("APP_PORT")
            or "8501"
        )
        app_port_prompt = f"App port [{default_app_port}]: "
        entered_app_port = input(app_port_prompt).strip()
        app_port_str = entered_app_port or str(default_app_port)
        if not app_port_str.isdigit() or not (1 <= int(app_port_str) <= 65535):
            print("App port must be an integer in range 1-65535.")
            sys.exit(2)
        app_port = int(app_port_str)
        save_to_env(str(app_port), "APP_PORT")
        os.environ["APP_PORT"] = str(app_port)

        # Safeguard: avoid ambiguous behavior when the target port is already occupied.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", app_port)) == 0:
                print(
                    f"APP_PORT {app_port} is already in use on the remote host. "
                    "Choose another APP_PORT or stop the running service."
                )
                sys.exit(2)

        default_host = (load_from_env("SSH_HOST") or "ucloud@ssh.cloud.sdu.dk").strip()
        default_port = (load_from_env("SSH_PORT") or "22").strip()

        host_prompt = f"SSH host/user (user@host){f' [{default_host}]' if default_host else ''}: "
        entered_host = input(host_prompt).strip()
        ssh_host = entered_host or default_host
        if not ssh_host:
            print("SSH host is required for ssh mode.")
            sys.exit(2)

        port_prompt = f"SSH port [{default_port}]: "
        entered_port = input(port_prompt).strip()
        ssh_port = entered_port or default_port
        if not str(ssh_port).isdigit() or not (1 <= int(ssh_port) <= 65535):
            print("SSH port must be an integer in range 1-65535.")
            sys.exit(2)

        save_to_env(ssh_host, "SSH_HOST")
        save_to_env(ssh_port, "SSH_PORT")

        cmd = f"ssh -N -L {app_port}:localhost:{app_port} {ssh_host} -p {ssh_port}"
        sep = "=" * 60
        print(f"\n{sep}")
        print("SSH TUNNEL REQUIRED (run this on your LOCAL machine)")
        print(sep)
        print("\n1) Start tunnel:")
        print(f"   {cmd}")
        print("\n2) Keep that terminal open.")
        print(f"\n3) Open Streamlit in your local browser:\n   http://localhost:{app_port}")
        print("\nIf the page does not load:")
        print("- Confirm tunnel is still running")
        print("- Confirm remote editor is running on the same port")
        print()
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
