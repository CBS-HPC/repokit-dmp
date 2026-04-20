"""Unified CLI entrypoint for repokit-dmp."""

from __future__ import annotations

import argparse
import sys


def _dispatch(func, argv: list[str], prog: str) -> None:
    old_argv = sys.argv
    try:
        sys.argv = [prog, *argv]
        func()
    finally:
        sys.argv = old_argv


def main() -> None:
    parser = argparse.ArgumentParser(prog="repokit-dmp", description="repokit-dmp commands")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ["dataset", "dcas-migration", "update", "editor", "init"]:
        p = sub.add_parser(name)
        p.add_argument("args", nargs=argparse.REMAINDER)

    ns = parser.parse_args()

    if ns.command == "dataset":
        from . import dataset

        _dispatch(dataset.main, ns.args, "repokit-dmp dataset")
    elif ns.command == "dcas-migration":
        from . import dcas

        _dispatch(dcas.main, ns.args, "repokit-dmp dcas-migration")
    elif ns.command == "update":
        from . import dmp

        _dispatch(dmp.main, ns.args, "repokit-dmp update")
    elif ns.command == "editor":
        from . import editor

        _dispatch(editor.cli, ns.args, "repokit-dmp editor")
    elif ns.command == "init":
        from . import init

        _dispatch(init.main, ns.args, "repokit-dmp init")


if __name__ == "__main__":
    main()
