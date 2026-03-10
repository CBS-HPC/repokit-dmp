"""Utilities for repokit-dmp."""

from __future__ import annotations

import os
import pathlib
import repokit_common


def ensure_project_root(root: str | pathlib.Path | None = None) -> pathlib.Path:
    """
    Set PROJECT_ROOT for standalone use.
    Also updates repokit-common modules that cache PROJECT_ROOT at import time.
    """
    root_path = pathlib.Path(root).resolve() if root is not None else pathlib.Path.cwd().resolve()

    repokit_common.PROJECT_ROOT = root_path

    # Keep cached module-level PROJECT_ROOT values aligned.
    try:
        import repokit_common.base as _base

        _base.PROJECT_ROOT = root_path
    except Exception:
        pass
    try:
        import repokit_common.tomlutils as _tomlutils

        _tomlutils.PROJECT_ROOT = root_path
    except Exception:
        pass

    return root_path


def bootstrap_runtime_root(root: str | pathlib.Path | None = None, chdir: bool = True) -> pathlib.Path:
    """
    Resolve and apply runtime project root consistently for CLI entrypoints.
    """
    root_path = ensure_project_root(root)
    if chdir:
        os.chdir(str(root_path))
    return root_path


__all__ = ["ensure_project_root", "bootstrap_runtime_root"]
