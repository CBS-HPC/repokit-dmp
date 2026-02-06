"""Utilities for repokit-dmp."""

from __future__ import annotations

import pathlib
import repokit_common


def ensure_project_root() -> None:
    """Set PROJECT_ROOT to the current working directory for standalone use."""
    repokit_common.PROJECT_ROOT = pathlib.Path.cwd().resolve()


__all__ = ["ensure_project_root"]
