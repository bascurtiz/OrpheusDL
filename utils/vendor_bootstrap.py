"""Utilities for making bundled third-party packages importable."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List


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


