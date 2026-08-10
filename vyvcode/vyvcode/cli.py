"""VyvCode CLI entry point.

``vyvcode`` with no arguments starts the REPL (built in B4). Flags:

    --version         print version and exit
    --probe           1-token liveness probe per role (B2)
    --install-assets  copy agent defs + skills into the target project (B5)
"""

from __future__ import annotations

import argparse
import os
import sys

from vyvcode import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vyvcode", description=__doc__)
    parser.add_argument("--version", action="version", version=f"vyvcode {__version__}")
    parser.add_argument(
        "--probe", action="store_true", help="probe each role's model and exit"
    )
    parser.add_argument(
        "--install-assets",
        action="store_true",
        help="install VyvCode agent definitions and skills into the current project",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="target project directory (default: current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    args = build_parser().parse_args(argv)

    from vyvcode.config import load_config

    cfg = load_config(args.project)

    if args.probe:
        from vyvcode.models import render_probe_table, run_probe

        results = run_probe(cfg)
        print(render_probe_table(results))
        return 0 if all(r.ok for r in results) else 1

    if args.install_assets:
        from vyvcode.installer import install_assets

        for line in install_assets(cfg):
            print(line)
        return 0

    from vyvcode.repl import run_repl

    return run_repl(cfg)


if __name__ == "__main__":
    sys.exit(main())
