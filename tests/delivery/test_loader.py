"""The plugin loader (vts-j8gz, part A).

Runs as a bootstrap step before api and worker start, so its failure modes
matter more than its happy path: if it exits non-zero the pod does not come
up, and transcription dies along with a delivery feature the user may not even
be using. Almost every test here is about degrading instead of failing.

No network: the GitHub client is a seam, replaced by a fake. `pip install` is
exercised for real against a locally built wheel, because "we shell out to pip
correctly" is exactly the part a mock would not prove.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

from vts.delivery import loader
from vts.delivery.loader import Asset, LoaderConfigError, Release


class _Settings:
    def __init__(self, cache_dir, sources=None):
        self.delivery_plugin_cache_dir = cache_dir
        self.delivery_plugin_sources = sources or []


class FakeGitHub:
    """Stands in for GitHubReleases. Records what was asked for."""

    def __init__(self, token=None, *, releases=None, payloads=None, fail_with=None):
        self.token = token
        self._releases = releases or {}
        self._payloads = payloads or {}
        self._fail_with = fail_with
        self.downloaded: list[str] = []

    def latest_release(self, repo):
        if self._fail_with is not None:
            raise self._fail_with
        return self._releases[repo]

    def download(self, asset, destination):
        self.downloaded.append(asset.name)
        destination.write_bytes(self._payloads[asset.name])


def _factory(**kwargs):
    """Build a client_factory that hands the same fake to every source."""
    made: list[FakeGitHub] = []

    def factory(token):
        client = FakeGitHub(token, **kwargs)
        made.append(client)
        return client

    factory.made = made
    return factory


@pytest.fixture
def wheel(tmp_path_factory):
    """A real, installable wheel — so the pip step is genuinely exercised."""
    src = tmp_path_factory.mktemp("plugin-src")
    (src / "dummy_plugin").mkdir()
    (src / "dummy_plugin" / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (src / "pyproject.toml").write_text(
        '[project]\nname = "dummy-plugin"\nversion = "0.1.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["dummy_plugin"]\n',
        encoding="utf-8",
    )
    out = tmp_path_factory.mktemp("plugin-dist")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(src)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return next(out.glob("*.whl"))


def _release_for(wheel: Path, *, tag="release-1"):
    payload = wheel.read_bytes()
    digest = loader._sha256(wheel)
    asset = Asset(name=wheel.name, url=f"https://api.example/assets/{wheel.name}", digest=digest)
    return Release(tag=tag, assets=[asset]), {wheel.name: payload}


# --- nothing configured ----------------------------------------------------


def test_no_sources_installs_nothing(tmp_path):
    settings = _Settings(tmp_path / "cache", sources=[])
    assert loader.run(settings, client_factory=_factory()) == 0
    # A base image with no plugins is a normal state, so nothing is created.
    assert not (tmp_path / "cache" / "site").exists()


# --- the happy path --------------------------------------------------------


def test_installs_a_wheel_and_records_it(tmp_path, wheel):
    release, payloads = _release_for(wheel)
    factory = _factory(releases={"o/r": release}, payloads=payloads)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])

    assert loader.run(settings, client_factory=factory) == 1

    site = tmp_path / "cache" / "site"
    assert (site / "dummy_plugin" / "__init__.py").exists(), "wheel was not installed"

    manifest = json.loads((tmp_path / "cache" / "manifest.json").read_text())
    # Keyed by distribution since the cache-hygiene fix; the wheel filename is
    # recorded inside the entry rather than used as the key.
    entry = manifest[loader.distribution_name(wheel.name)]
    assert entry["digest"] == loader._sha256(wheel)
    assert entry["source_repo"] == "o/r"
    assert entry["release"] == "release-1"


def test_matching_digest_skips_without_downloading(tmp_path, wheel):
    """The reason the manifest exists: a restart must not re-download and
    re-install every wheel it already has."""
    release, payloads = _release_for(wheel)
    factory = _factory(releases={"o/r": release}, payloads=payloads)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])

    assert loader.run(settings, client_factory=factory) == 1
    first = factory.made[0].downloaded
    assert first == [wheel.name]

    factory2 = _factory(releases={"o/r": release}, payloads=payloads)
    assert loader.run(settings, client_factory=factory2) == 0
    assert factory2.made[0].downloaded == [], "a known digest must not be re-downloaded"


def test_changed_digest_reinstalls(tmp_path, wheel):
    """A rebuilt release under the same asset name must be picked up — that is
    why the manifest keys on digest rather than on file name."""
    release, payloads = _release_for(wheel)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    assert loader.run(settings, client_factory=_factory(releases={"o/r": release}, payloads=payloads)) == 1

    # Same name, different content.
    manifest_path = tmp_path / "cache" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[loader.distribution_name(wheel.name)]["digest"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    factory = _factory(releases={"o/r": release}, payloads=payloads)
    assert loader.run(settings, client_factory=factory) == 1
    assert factory.made[0].downloaded == [wheel.name]


# --- integrity -------------------------------------------------------------


def test_digest_mismatch_refuses_to_install(tmp_path, wheel):
    """Bytes that do not match what the release advertises are not installed
    and not recorded — otherwise a corrupt or swapped asset would be trusted
    forever after, since the manifest would claim it is present."""
    release, payloads = _release_for(wheel)
    lying = Release(
        tag=release.tag,
        assets=[Asset(name=wheel.name, url="https://api.example/a", digest="sha256:" + "b" * 64)],
    )
    factory = _factory(releases={"o/r": lying}, payloads=payloads)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])

    assert loader.run(settings, client_factory=factory) == 0
    assert not (tmp_path / "cache" / "site" / "dummy_plugin").exists()
    assert json.loads((tmp_path / "cache" / "manifest.json").read_text()) == {}


# --- degradation -----------------------------------------------------------


def test_unreachable_source_does_not_stop_the_others(tmp_path, wheel):
    """Per-source degradation: one dead source must not deny the rest."""
    release, payloads = _release_for(wheel)

    def factory(token):
        # The first source raises, the second serves the wheel.
        if not factory.calls:
            factory.calls.append("bad")
            return FakeGitHub(token, fail_with=urllib.error.URLError("network down"))
        return FakeGitHub(token, releases={"good/r": release}, payloads=payloads)

    factory.calls = []
    settings = _Settings(
        tmp_path / "cache",
        sources=[{"repo": "bad/r", "token_env": ""}, {"repo": "good/r", "token_env": ""}],
    )

    assert loader.run(settings, client_factory=factory) == 1
    assert (tmp_path / "cache" / "site" / "dummy_plugin").exists()


def test_release_without_wheels_is_not_an_error(tmp_path):
    empty = Release(tag="release-1", assets=[Asset(name="notes.txt", url="u", digest="")])
    factory = _factory(releases={"o/r": empty}, payloads={})
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    assert loader.run(settings, client_factory=factory) == 0


def test_source_without_repo_is_skipped(tmp_path):
    settings = _Settings(tmp_path / "cache", sources=[{"token_env": "X"}])
    assert loader.run(settings, client_factory=_factory()) == 0


# --- locking ---------------------------------------------------------------


def test_held_lock_skips_the_run(tmp_path):
    """Two concurrent `pip install --target` runs into one directory can
    interleave badly, so the loser backs off — the holder installs the same
    wheels anyway."""
    cache = tmp_path / "cache"
    (cache / "site").mkdir(parents=True)
    (cache / ".lock").write_text("999999")

    settings = _Settings(cache, sources=[{"repo": "o/r", "token_env": ""}])
    factory = _factory(releases={}, payloads={})
    assert loader.run(settings, client_factory=factory) == 0
    assert factory.made == [], "a held lock must stop the run before any network call"


def test_lock_is_released_even_when_a_source_fails(tmp_path):
    """A lock left behind would wedge every subsequent start."""
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    factory = _factory(fail_with=urllib.error.URLError("boom"))
    loader.run(settings, client_factory=factory)
    assert not (tmp_path / "cache" / ".lock").exists()


# --- operator-fixable failures --------------------------------------------


def test_unwritable_cache_is_fatal(tmp_path):
    """The one class worth stopping for: continuing would silently run without
    the plugins the operator configured."""
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory")
    settings = _Settings(blocker, sources=[{"repo": "o/r", "token_env": ""}])
    with pytest.raises(LoaderConfigError):
        loader.run(settings, client_factory=_factory())


# --- tokens ----------------------------------------------------------------


def test_token_is_read_from_the_named_env_var(tmp_path, wheel, monkeypatch):
    """The config names an env var; the token itself never appears in config."""
    monkeypatch.setenv("VTS_TEST_PLUGIN_TOKEN", "s3cr3t")
    release, payloads = _release_for(wheel)
    factory = _factory(releases={"o/r": release}, payloads=payloads)
    settings = _Settings(
        tmp_path / "cache",
        sources=[{"repo": "o/r", "token_env": "VTS_TEST_PLUGIN_TOKEN"}],
    )
    loader.run(settings, client_factory=factory)
    assert factory.made[0].token == "s3cr3t"


def test_public_source_gets_no_token(tmp_path, wheel):
    release, payloads = _release_for(wheel)
    factory = _factory(releases={"o/r": release}, payloads=payloads)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    loader.run(settings, client_factory=factory)
    assert factory.made[0].token is None


# --- manifest robustness ---------------------------------------------------


def test_corrupt_manifest_is_treated_as_empty(tmp_path, wheel):
    """Re-installing is idempotent; refusing to start is not an option."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text("{not json")

    release, payloads = _release_for(wheel)
    factory = _factory(releases={"o/r": release}, payloads=payloads)
    settings = _Settings(cache, sources=[{"repo": "o/r", "token_env": ""}])
    assert loader.run(settings, client_factory=factory) == 1


def test_manifest_write_is_atomic(tmp_path):
    """No .tmp left behind, so an interrupted write cannot be mistaken for
    the real manifest."""
    cache = tmp_path / "cache"
    cache.mkdir()
    loader.write_manifest(cache, {"a.whl": {"digest": "sha256:x"}})
    assert json.loads((cache / "manifest.json").read_text())["a.whl"]["digest"] == "sha256:x"
    assert not list(cache.glob("*.tmp"))


# --- entry point -----------------------------------------------------------


def test_main_exits_zero_when_a_source_is_unreachable(tmp_path, monkeypatch):
    """The whole reason for the soft exit: a bootstrap failure would take the
    pod — and transcription with it — down over an unreachable GitHub."""
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    # Patch the cached accessor's RESULT, not the function: conftest's
    # settings-isolation fixture calls get_settings.cache_clear(), which a
    # plain lambda does not have.
    monkeypatch.setattr("vts.core.config.Settings", lambda **_: settings)
    import vts.core.config as _cfg
    _cfg.get_settings.cache_clear()
    monkeypatch.setattr(
        loader, "GitHubReleases",
        lambda token=None: FakeGitHub(token, fail_with=urllib.error.URLError("down")),
    )
    assert loader.main() == 0


def test_main_exits_nonzero_on_operator_fixable_config(tmp_path, monkeypatch):
    blocker = tmp_path / "cache"
    blocker.write_text("not a directory")
    settings = _Settings(blocker, sources=[{"repo": "o/r", "token_env": ""}])
    monkeypatch.setattr("vts.core.config.Settings", lambda **_: settings)
    import vts.core.config as _cfg
    _cfg.get_settings.cache_clear()
    assert loader.main() == 1


# --- cache hygiene (vts-6o37 followup) --------------------------------------


def test_distribution_name_ignores_the_version():
    assert loader.distribution_name("vts_outline-0.2.0-py3-none-any.whl") == "vts_outline"
    assert loader.distribution_name("a_b-1.0.0rc1-py3-none-any.whl") == "a_b"


def test_upgrade_removes_the_previous_versions_metadata(tmp_path, wheel):
    """pip --target overwrites a package's CODE but leaves the old .dist-info
    behind, and importlib.metadata reads exactly that to find entry points.

    Harmless while the module and entry-point names hold; the moment a plugin
    renames either, the leftovers give a dangling entry point or a ghost
    adapter — silent failures in a registry that loads third-party code.
    """
    release, payloads = _release_for(wheel)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    assert loader.run(settings, client_factory=_factory(releases={"o/r": release}, payloads=payloads)) == 1

    site = tmp_path / "cache" / "site"
    # Fake a leftover from an earlier version of the same distribution.
    stale = site / "dummy_plugin-0.0.1.dist-info"
    stale.mkdir()
    (stale / "METADATA").write_text("Name: dummy-plugin\nVersion: 0.0.1\n")

    # Re-install (digest forced to differ so it does not skip).
    manifest_path = tmp_path / "cache" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dummy_plugin"]["digest"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    assert loader.run(settings, client_factory=_factory(releases={"o/r": release}, payloads=payloads)) == 1

    assert not stale.exists(), "stale .dist-info from the old version must be removed"
    remaining = sorted(p.name for p in site.glob("dummy_plugin-*.dist-info"))
    assert len(remaining) == 1, f"expected exactly one metadata dir, got {remaining}"


def test_purge_leaves_other_distributions_alone(tmp_path, wheel):
    """Several plugins share one site/ directory, so the cleanup must be
    scoped to the distribution being installed — deleting more broadly would
    take out a sibling plugin."""
    release, payloads = _release_for(wheel)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    loader.run(settings, client_factory=_factory(releases={"o/r": release}, payloads=payloads))

    site = tmp_path / "cache" / "site"
    sibling = site / "other_plugin-1.0.0.dist-info"
    sibling.mkdir()
    (sibling / "METADATA").write_text("Name: other-plugin\n")

    loader._purge_stale_metadata(site, "dummy_plugin")
    assert sibling.exists(), "another plugin's metadata must survive"


def test_manifest_holds_one_entry_per_distribution(tmp_path, wheel):
    """Keyed by distribution, not by wheel filename: the filename carries the
    version, so every release used to add an entry and the superseded one
    lingered — the manifest stopped answering "what is installed now"."""
    release, payloads = _release_for(wheel)
    settings = _Settings(tmp_path / "cache", sources=[{"repo": "o/r", "token_env": ""}])
    loader.run(settings, client_factory=_factory(releases={"o/r": release}, payloads=payloads))

    manifest = json.loads((tmp_path / "cache" / "manifest.json").read_text())
    assert list(manifest) == ["dummy_plugin"]
    assert manifest["dummy_plugin"]["wheel"] == wheel.name

    # A newer release of the SAME distribution replaces the entry rather than
    # adding a second one.
    # A different digest, as a genuinely new build would have — otherwise the
    # skip-if-present check correctly treats it as already installed.
    newer_bytes = wheel.read_bytes() + b"\n# rebuilt\n"
    newer_digest = "sha256:" + __import__("hashlib").sha256(newer_bytes).hexdigest()
    newer = Release(
        tag="release-2",
        assets=[Asset(name="dummy_plugin-9.9.9-py3-none-any.whl",
                      url="https://api.example/a", digest=newer_digest)],
    )
    loader.run(settings, client_factory=_factory(
        releases={"o/r": newer}, payloads={"dummy_plugin-9.9.9-py3-none-any.whl": newer_bytes}))

    manifest = json.loads((tmp_path / "cache" / "manifest.json").read_text())
    assert list(manifest) == ["dummy_plugin"], f"one entry per distribution, got {list(manifest)}"
    assert manifest["dummy_plugin"]["release"] == "release-2"
