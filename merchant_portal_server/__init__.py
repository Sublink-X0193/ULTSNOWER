"""Repo-root import shim so `python -m merchant_portal_server` works before install."""

from pathlib import Path

_src_pkg = Path(__file__).resolve().parent.parent / "src" / "merchant_portal_server"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))  # type: ignore[name-defined]

__version__ = "0.1.0"
