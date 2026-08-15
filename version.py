"""Single source of truth for the running app version.

In a py2app bundle the version comes from Info.plist (written by setup.py at
build time). In a dev checkout it comes from the VERSION file. Both are fed
from the same VERSION file, so they can never drift.
"""

import plistlib
import sys
from pathlib import Path


def _from_bundle() -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    plist = Path(sys.executable).parent.parent / "Info.plist"
    try:
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        return str(data.get("CFBundleShortVersionString") or "") or None
    except Exception:
        return None


def _from_file() -> str | None:
    path = Path(__file__).parent / "VERSION"
    try:
        return path.read_text().strip() or None
    except Exception:
        return None


def current() -> str:
    return _from_bundle() or _from_file() or "0.0.0"


def as_tuple(version: str) -> tuple[int, ...]:
    """Parse "1.2.10" -> (1, 2, 10). Unparsable parts become 0."""
    parts = []
    for chunk in str(version).strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def is_newer(candidate: str, than: str) -> bool:
    a, b = as_tuple(candidate), as_tuple(than)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


__version__ = current()
