"""Bootstrap a repokit-dmp project."""

from __future__ import annotations

import argparse
from pathlib import Path

from repokit_common import read_toml, write_toml

from . import bootstrap_runtime_root
from .dmp import DEFAULT_DMP_PATH, create_or_update_dmp_from_schema


DEFAULT_DATASET_PATTERNS = "data/*"
DEFAULT_DATA_POLICY_DESCRIPTION = (
    "Agent data-access policy: paths with sensitive/proprietary data "
    "that must be handled with restricted access and synced to agent ignore files."
)


def _ensure_dataset_policy(root_path: Path) -> None:
    datasets_cfg = (
        read_toml(
            folder=str(root_path),
            json_filename=None,
            tool_name="datasets",
            toml_path="pyproject.toml",
        )
        or {}
    )
    patterns = datasets_cfg.get("patterns")
    if isinstance(patterns, list):
        patterns = [p for p in patterns if isinstance(p, str) and p.strip()]
    elif isinstance(patterns, str):
        patterns = patterns.strip() or DEFAULT_DATASET_PATTERNS
    else:
        patterns = DEFAULT_DATASET_PATTERNS

    write_toml(
        data={"patterns": patterns},
        folder=str(root_path),
        json_filename=None,
        tool_name="datasets",
        toml_path="pyproject.toml",
    )


def _ensure_data_policy(root_path: Path) -> None:
    data_policy_cfg = (
        read_toml(
            folder=str(root_path),
            json_filename=None,
            tool_name="data_policy",
            toml_path="pyproject.toml",
        )
        or {}
    )
    patterns = data_policy_cfg.get("patterns")
    if isinstance(patterns, str):
        patterns = [patterns.strip()] if patterns.strip() else []
    elif isinstance(patterns, list):
        patterns = [p for p in patterns if isinstance(p, str) and p.strip()]
    else:
        patterns = []

    payload = {
        "tool-description": data_policy_cfg.get("tool-description")
        or DEFAULT_DATA_POLICY_DESCRIPTION,
        "patterns": patterns,
    }

    write_toml(
        data=payload,
        folder=str(root_path),
        json_filename=None,
        tool_name="data_policy",
        toml_path="pyproject.toml",
    )


def init_project(force: bool = False) -> dict[str, object]:
    """Initialize project-local DMP scaffolding."""
    root_path = bootstrap_runtime_root()
    pyproject_path = root_path / "pyproject.toml"
    dmp_path = root_path / DEFAULT_DMP_PATH

    _ensure_dataset_policy(root_path)
    _ensure_data_policy(root_path)

    created_dmp = False
    if force or not dmp_path.exists():
        create_or_update_dmp_from_schema(dmp_path=dmp_path)
        created_dmp = True

    return {
        "root_path": root_path,
        "pyproject_path": pyproject_path,
        "dmp_path": dmp_path,
        "created_dmp": created_dmp,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="repokit-dmp init", description="Bootstrap a project")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild dmp.json even if it already exists.",
    )
    args = parser.parse_args()

    result = init_project(force=args.force)
    root_path = result["root_path"]
    pyproject_path = result["pyproject_path"]
    dmp_path = result["dmp_path"]
    created_dmp = result["created_dmp"]

    print(f"[INFO] Initialized repokit-dmp project in {root_path}")
    print(f"[INFO] pyproject.toml ensured at {pyproject_path}")
    if created_dmp:
        print(f"[INFO] dmp.json created or updated at {dmp_path}")
    else:
        print(f"[INFO] dmp.json left unchanged at {dmp_path}")


if __name__ == "__main__":
    main()
