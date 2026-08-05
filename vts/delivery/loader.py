"""Install delivery adapter plugins from GitHub Releases (vts-j8gz).

A VTS image ships with no adapters. This runs as a bootstrap step before the
api and worker start — an initContainer in the pod — and installs the wheels
attached to the latest release of each configured source into a shared cache
directory that both containers carry on their PYTHONPATH.

Two properties shape almost every decision here:

* **It must not stop VTS from starting.** A bootstrap that fails takes the pod
  down with it, so an unreachable GitHub would cost transcription too — for a
  feature the user may not even be using. Network, auth and install failures
  degrade to "use what is already cached" and exit 0. Only operator-fixable
  problems (an unwritable cache directory, unreadable config) exit non-zero.

* **Failure is per-source.** One broken source must not deny the others; the
  loop continues and reports each failure separately.

The module knows nothing about adapters or the contract. It puts packages on
disk; the registry validates whatever Python ends up importing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DOWNLOAD_TIMEOUT = 120
_API_TIMEOUT = 30


class LoaderConfigError(RuntimeError):
    """Operator-fixable: bad config, or a cache directory we cannot write."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    digest: str  # "sha256:<hex>" as returned by the Releases API, may be ""


@dataclass(frozen=True)
class Release:
    tag: str
    assets: list[Asset]


class GitHubReleases:
    """The only part that talks to the network — the seam tests replace."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def _request(self, url: str, *, accept: str, timeout: int):
        request = urllib.request.Request(url)
        request.add_header("Accept", accept)
        request.add_header("User-Agent", "vts-plugin-loader")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310

    def latest_release(self, repo: str) -> Release:
        url = f"{_GITHUB_API}/repos/{repo}/releases/latest"
        with self._request(url, accept="application/vnd.github+json", timeout=_API_TIMEOUT) as response:
            payload = json.loads(response.read())
        assets = [
            Asset(
                name=asset["name"],
                # The API url, not browser_download_url: only the former
                # accepts a token, which a private source needs.
                url=asset["url"],
                digest=asset.get("digest") or "",
            )
            for asset in payload.get("assets", [])
        ]
        return Release(tag=payload.get("tag_name", ""), assets=assets)

    def download(self, asset: Asset, destination: Path) -> None:
        with self._request(
            asset.url, accept="application/octet-stream", timeout=_DOWNLOAD_TIMEOUT
        ) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def read_manifest(cache_dir: Path) -> dict[str, Any]:
    """Installed assets, keyed by asset name, with the digest we installed.

    A corrupt manifest is treated as empty rather than fatal: the cost is
    re-installing wheels that were already there, which is idempotent, while
    refusing to start would be much worse.
    """
    path = cache_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("plugin manifest unreadable (%s); treating as empty", exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(cache_dir: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest atomically, so an interrupted run cannot leave a
    half-written file that the next start would have to discard."""
    path = cache_dir / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def distribution_name(wheel_name: str) -> str:
    """Distribution a wheel belongs to, from its filename.

    Wheel names are `{distribution}-{version}-{python}-{abi}-{platform}.whl`
    with the distribution normalised to underscores, so everything before the
    first hyphen identifies the package regardless of version.
    """
    return wheel_name.split("-", 1)[0]


def _purge_stale_metadata(site_dir: Path, distribution: str) -> None:
    """Remove `.dist-info` left by earlier versions of this distribution.

    `pip install --target --upgrade` overwrites a package's CODE but does not
    remove the previous version's metadata: in --target mode pip keeps no
    record of what is installed and cannot uninstall. The leftovers accumulate
    and are read by `importlib.metadata`, which is how entry points are found.

    Harmless while a package keeps the same module and entry-point name — the
    stale metadata points at code that has already been overwritten. It stops
    being harmless the moment a plugin renames its module (the entry point
    dangles) or its entry point (a ghost adapter appears alongside the real
    one). Both are silent failures in a registry that loads third-party code.

    Deliberately scoped to the distribution being installed: deleting anything
    broader would take out a sibling plugin sharing the same directory.
    """
    for stale in site_dir.glob(f"{distribution}-*.dist-info"):
        if not stale.is_dir():
            continue
        try:
            shutil.rmtree(stale)
            logger.info("removed stale plugin metadata %s", stale.name)
        except OSError as exc:
            # Not fatal: the fresh install still lands, and a leftover
            # directory is the state we were already living with.
            logger.warning("could not remove stale metadata %s: %s", stale.name, exc)


def install_wheel(wheel: Path, site_dir: Path) -> None:
    """`pip install --target`, with --no-deps.

    --no-deps is deliberate: a wheel is expected to be self-sufficient against
    the VTS image. Pulling transitive dependencies into the cache risks
    shadowing a different version of something the image already provides, and
    adds a second way for the bootstrap to fail. Plugins vendor what they need.
    """
    # Before, not after: a failed install must not leave the package without
    # metadata, which would make it invisible to entry-point discovery.
    _purge_stale_metadata(site_dir, distribution_name(wheel.name))
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", str(site_dir),
            "--no-deps",
            "--upgrade",
            str(wheel),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pip install failed for {wheel.name}: {result.stderr.strip()[:500]}"
        )


def _install_source(
    source: dict[str, str],
    *,
    client: GitHubReleases,
    site_dir: Path,
    manifest: dict[str, Any],
) -> int:
    """Install one source's wheels. Returns how many were installed."""
    repo = source["repo"]
    release = client.latest_release(repo)
    wheels = [asset for asset in release.assets if asset.name.endswith(".whl")]
    if not wheels:
        logger.warning("plugin source %s: latest release %r has no .whl assets", repo, release.tag)
        return 0

    installed = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for asset in wheels:
            # Keyed by DISTRIBUTION, not by asset filename: the filename
            # carries the version, so a new release wrote a second entry and
            # the superseded one lingered forever. The manifest is supposed to
            # answer "what is installed now", and with one entry per file it
            # could not.
            distribution = distribution_name(asset.name)
            recorded = manifest.get(distribution, {})
            if asset.digest and recorded.get("digest") == asset.digest:
                logger.info("plugin %s already installed (%s)", asset.name, asset.digest[:19])
                continue

            target = Path(tmpdir) / asset.name
            client.download(asset, target)

            actual = _sha256(target)
            if asset.digest and actual != asset.digest:
                # Do NOT install and do NOT record: a mismatch means the bytes
                # are not what the release advertises.
                raise RuntimeError(
                    f"digest mismatch for {asset.name}: "
                    f"release says {asset.digest}, downloaded {actual}"
                )

            install_wheel(target, site_dir)
            # Recorded only after a successful install, so an interrupted run
            # never leaves a half-installed wheel marked as present.
            manifest[distribution] = {
                "wheel": asset.name,
                "digest": asset.digest or actual,
                "release": release.tag,
                "source_repo": repo,
                "installed_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            installed += 1
            logger.info("installed plugin %s from %s (%s)", asset.name, repo, release.tag)
    return installed


def run(settings: Any, *, client_factory=GitHubReleases) -> int:
    """Install every configured source. Returns the number of wheels installed.

    Raises LoaderConfigError for operator-fixable problems only; everything
    else is logged and skipped so VTS still starts.
    """
    sources = list(getattr(settings, "delivery_plugin_sources", []) or [])
    if not sources:
        logger.info("no delivery plugin sources configured; nothing to install")
        return 0

    cache_dir = Path(settings.delivery_plugin_cache_dir)
    site_dir = cache_dir / "site"
    try:
        site_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LoaderConfigError(f"cannot create plugin cache at {site_dir}: {exc}") from exc

    lock_path = cache_dir / ".lock"
    try:
        # O_EXCL: another bootstrap holds it. Two concurrent `pip install
        # --target` runs into one directory can interleave badly, so the loser
        # backs off rather than racing — the winner installs the same wheels.
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        logger.warning("plugin loader lock %s is held; skipping this run", lock_path)
        return 0
    except OSError as exc:
        raise LoaderConfigError(f"cannot take plugin lock {lock_path}: {exc}") from exc

    try:
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)

        manifest = read_manifest(cache_dir)
        installed = 0
        for source in sources:
            repo = (source or {}).get("repo") if isinstance(source, dict) else None
            if not repo:
                logger.error("plugin source %r has no 'repo'; skipping", source)
                continue
            token_env = (source.get("token_env") or "").strip()
            token = os.environ.get(token_env) if token_env else None
            if token_env and not token:
                logger.warning(
                    "plugin source %s names token env %s, which is unset — "
                    "trying anonymously", repo, token_env,
                )
            try:
                installed += _install_source(
                    source,
                    client=client_factory(token),
                    site_dir=site_dir,
                    manifest=manifest,
                )
            except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
                # Per-source degradation: whatever is already in the cache
                # stays usable, and the remaining sources still get a turn.
                logger.error("plugin source %s failed: %s — using cache", repo, exc)
                continue

        write_manifest(cache_dir, manifest)
        return installed
    finally:
        try:
            lock_path.unlink()
        except OSError:
            logger.warning("could not release plugin lock %s", lock_path)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from vts.core.config import get_settings

    try:
        settings = get_settings()
        installed = run(settings)
    except LoaderConfigError as exc:
        # The one class of failure worth stopping for: an operator can fix it,
        # and continuing would silently run without the plugins they configured.
        logger.error("plugin loader cannot run: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - bootstrap must not break the pod
        logger.exception("plugin loader failed unexpectedly: %s — starting anyway", exc)
        return 0

    logger.info("plugin loader finished; %d wheel(s) installed", installed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
