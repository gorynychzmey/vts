#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


def read_version() -> str:
    init_path = Path(__file__).resolve().parents[1] / "vts" / "__init__.py"
    content = init_path.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', content)
    if not match:
        raise SystemExit("Unable to parse version from vts/__init__.py")
    return match.group(1)


print(read_version())
