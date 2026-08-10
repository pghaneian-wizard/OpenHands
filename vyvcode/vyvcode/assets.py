"""Locate the packaged agent definitions and skills.

Two layouts exist: the repo checkout keeps ``agents/`` and ``skills/`` beside
the ``vyvcode`` package; the installed wheel ships them inside it (hatch
force-include in pyproject.toml). Resolve at import time so a broken install
fails loudly at startup, not mid-pipeline.
"""

from __future__ import annotations

from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent


def assets_root(pkg_dir: Path | None = None) -> Path:
    """Directory containing ``agents/`` and ``skills/`` for this install."""
    pkg = pkg_dir if pkg_dir is not None else _PKG_DIR
    if (pkg / "skills").is_dir():
        return pkg  # installed wheel: assets inside the package
    return pkg.parent  # repo checkout: assets beside the package
