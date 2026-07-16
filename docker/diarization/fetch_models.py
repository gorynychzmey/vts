"""Vendor pyannote weights at build time, verified by hash.

Runtime must never reach out to Hugging Face: the community mirror is run by a
single unverified account, so the hashes pinned here are the only thing standing
between a build and a supply-chain surprise. A mismatch fails the build.

Hashes verified anonymously (no token, HTTP 200) on 2026-07-16, matching the
sizes recorded during the 2026-07-15 stack research.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx

_BASE = "https://huggingface.co/pyannote-community/speaker-diarization-community-1/resolve/main"

# config.yaml wires the pipeline together with $model-relative paths, and plda/
# carries the VBx clustering parameters. The two .bin files alone do not make a
# loadable pipeline.
MODELS = {
    "config.yaml": {
        "size": 444,
        "sha256": "5ce2bfa9a938dc132cec1172592d65173cbb8f444ea1e4133f10f9391de155be",
    },
    "segmentation/pytorch_model.bin": {
        "size": 5_906_507,
        "sha256": "7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e",
    },
    "embedding/pytorch_model.bin": {
        "size": 26_646_242,
        "sha256": "6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929",
    },
    "plda/plda.npz": {
        "size": 133_852,
        "sha256": "9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255",
    },
    "plda/xvec_transform.npz": {
        "size": 134_376,
        "sha256": "325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f",
    },
}


def fetch(name: str, spec: dict, target_dir: Path) -> None:
    target = target_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", f"{_BASE}/{name}", follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as handle:
            for block in response.iter_bytes():
                digest.update(block)
                size += len(block)
                handle.write(block)
    if size != spec["size"]:
        raise SystemExit(f"{name}: size {size} != expected {spec['size']}")
    if digest.hexdigest() != spec["sha256"]:
        raise SystemExit(f"{name}: sha256 {digest.hexdigest()} != expected {spec['sha256']}")
    print(f"{name}: verified {size} bytes")


if __name__ == "__main__":
    target_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/models")
    for name, spec in MODELS.items():
        fetch(name, spec, target_dir)
