#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bump vts semantic version.")
    parser.add_argument(
        "part",
        choices=("patch", "minor", "major"),
        help="Version part to bump.",
    )
    return parser.parse_args()


def bump(part: str, major: int, minor: int, patch: int) -> tuple[int, int, int]:
    if part == "patch":
        return major, minor, patch + 1
    if part == "minor":
        return major, minor + 1, 0
    return major + 1, 0, 0


def main() -> None:
    args = parse_args()
    init_path = Path("vts/__init__.py")
    content = init_path.read_text(encoding="utf-8")
    match = VERSION_RE.search(content)
    if not match:
        raise SystemExit("Unable to locate __version__ in vts/__init__.py")

    current = tuple(int(x) for x in match.groups())
    next_version = bump(args.part, *current)
    replacement = f'__version__ = "{next_version[0]}.{next_version[1]}.{next_version[2]}"'
    updated = VERSION_RE.sub(replacement, content, count=1)
    init_path.write_text(updated, encoding="utf-8")
    print(f"{current[0]}.{current[1]}.{current[2]} -> {next_version[0]}.{next_version[1]}.{next_version[2]}")


if __name__ == "__main__":
    main()

