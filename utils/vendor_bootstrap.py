"""Utilities for making bundled third-party packages importable."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List, Optional, Union

PathLike = Union[str, Path]


def _candidate_roots() -> List[Path]:
    """Return possible root directories that may contain the vendor folder."""
    candidates: List[Path] = []

    # When the app is frozen (PyInstaller), prefer the executable location and
    # _MEIPASS extraction directory.
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        candidates.append(exe_path.parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))

    # Project source tree (utils/.. = repo root)
    candidates.append(Path(__file__).resolve().parents[1])
    # Current working directory as a fall-back
    candidates.append(Path.cwd())

    # De-duplicate while preserving order.
    unique: List[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _insert_path(path: Path, inserted: List[Path]) -> None:
    """Insert a path into sys.path if it is not already present."""
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
        inserted.append(path)


def bootstrap_vendor_paths() -> List[Path]:
    """Ensure vendorised third-party packages are available on sys.path."""
    inserted: List[Path] = []
    for root in _candidate_roots():
        vendor_dir = root / "vendor"
        if not vendor_dir.exists():
            continue

        # Add the vendor directory itself plus every immediate child directory.
        _insert_path(vendor_dir, inserted)
        for child in vendor_dir.iterdir():
            if child.is_dir():
                _insert_path(child, inserted)
    return inserted


def resolve_gamdl_sys_path(modules_applemusic_dir: PathLike) -> Optional[Path]:
    """Return the sys.path entry needed for ``import gamdl`` without shadowing ``utils``.

    Supports both embedded layouts:
    - nested repo: ``modules/applemusic/gamdl/gamdl/__init__.py``
    - flat module: ``modules/applemusic/gamdl/__init__.py``
    """
    root = Path(modules_applemusic_dir)
    wrapper_root = root / "gamdl"
    nested_pkg = wrapper_root / "gamdl" / "__init__.py"
    flat_pkg = wrapper_root / "__init__.py"

    if nested_pkg.is_file():
        return wrapper_root
    if flat_pkg.is_file():
        # Use the Apple Music module root so ``gamdl/utils.py`` does not register as top-level ``utils``.
        return root
    return None


def resolve_gamdl_package_root(modules_applemusic_dir: PathLike) -> Optional[Path]:
    """Return the directory that contains the ``gamdl`` package sources."""
    root = Path(modules_applemusic_dir)
    wrapper_root = root / "gamdl"
    nested_pkg = wrapper_root / "gamdl"
    flat_pkg = wrapper_root

    if (nested_pkg / "__init__.py").is_file():
        return nested_pkg
    if (flat_pkg / "__init__.py").is_file():
        return flat_pkg
    return None


def insert_gamdl_sys_path(modules_applemusic_dir: PathLike) -> Optional[Path]:
    """Insert the correct gamdl import path if the Apple Music module is present."""
    candidate = resolve_gamdl_sys_path(modules_applemusic_dir)
    if candidate is not None:
        inserted: List[Path] = []
        _insert_path(candidate, inserted)
    return candidate


