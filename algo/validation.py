"""Compatibility imports for the repository-wide value validators."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.validation import require_float, require_int  # noqa: E402,F401

__all__ = ("require_float", "require_int")
