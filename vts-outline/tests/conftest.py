"""Make `vts_outline` importable when running from the repo root.

The package is a standalone distribution (`pip install ./vts-outline`), but the
repo-root pytest run does not have it installed, so put its source dir on
`sys.path` for the test session.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
