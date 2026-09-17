"""The container wrapper around scripts/prodq.py (vts-axm0).

The same long `podman run` line was retyped at least 21 times in one day, in
four slightly different spellings, and one of those spellings was wrong and had
to be run again. The wrapper exists so the invocation is one line and the same
line every time.

Everything site-specific — the image, the mounts, whether sudo is needed — comes
from the environment, and the tests below pin that: this repository is PUBLIC,
so a real image name or host path committed here would be an infrastructure map.
The wrapper must therefore be unusable without an environment that the operator
supplies, and must say so plainly rather than falling back to a guess.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "prodq-container.sh"


@pytest.fixture
def argv_recorder(tmp_path: Path) -> Path:
    """A stand-in container engine that prints its argv, one per line.

    `echo` would flatten the arguments into one string, which is exactly the
    boundary these tests are about: the SQL has to arrive as a SINGLE argument
    however many spaces and quotes it contains.
    """
    recorder = tmp_path / "fake-engine"
    recorder.write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\n" "$a"; done\n',
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    return recorder


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    full_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        # Never read the operator's own config during a test.
        "VTS_PRODQ_CONFIG": "/nonexistent/prodq.env",
    }
    full_env.update(env)
    return subprocess.run(
        [str(WRAPPER), *args], capture_output=True, text=True, env=full_env, timeout=30,
    )


def test_the_wrapper_exists_and_is_executable():
    assert WRAPPER.exists(), "scripts/prodq-container.sh is missing"
    assert os.access(WRAPPER, os.X_OK), "the wrapper is not executable"


def test_it_refuses_to_guess_the_image():
    """No default image, and the message names what to set.

    A default would either be wrong everywhere but one machine, or it would be
    that machine's image name — committed to a public repository.
    """
    result = _run(["SELECT 1"], {"VTS_PRODQ_ENGINE": "/bin/true"})
    assert result.returncode != 0
    assert "VTS_PRODQ_IMAGE" in result.stderr


def test_it_refuses_an_empty_invocation():
    result = _run([], {"VTS_PRODQ_IMAGE": "example.invalid/app:test"})
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()


def test_the_sql_arrives_as_one_argument(argv_recorder: Path):
    """The quoting trap prodq.py was written to remove, one layer out.

    A statement containing spaces and a quoted literal must reach the container
    as a single argv element; split, it becomes a different query or a syntax
    error.
    """
    sql = "SELECT id FROM tasks WHERE source_title = 'a b c' LIMIT 1"
    result = _run([sql], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
    })
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert sql in argv, f"the statement was not passed as one argument: {argv}"


def test_it_runs_the_repo_copy_of_prodq_inside_the_image(argv_recorder: Path):
    """The script under test is THIS checkout's, mounted read-only.

    That is the whole point of running it in the image: the code is the one
    being edited, the dependencies are the image's. The path is derived from
    the wrapper's own location, so no absolute path is written down.
    """
    result = _run(["SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
    })
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert "run" in argv and "--rm" in argv

    # Checked against the FILE ON DISK, not only against a path this test
    # computed the same way the wrapper does. A test that derives the expected
    # key by the code's own rule agrees with the code and can disagree with
    # reality — that exact shape hid a silent bug in rules-changed-notice.sh on
    # 2026-09-17 (see the feedback memory on self-confirming fixtures).
    mount_specs = [a for a in argv if ":/app/scripts/prodq.py:" in a]
    assert mount_specs, f"nothing is mounted at /app/scripts/prodq.py: {argv}"
    source = Path(mount_specs[0].split(":")[0])
    assert source.is_file(), (
        f"the wrapper mounts {source}, which does not exist — the path it builds "
        f"is wrong, however plausible it looks"
    )
    assert source.read_text(encoding="utf-8").startswith("#!"), (
        f"{source} is not the prodq script"
    )
    assert source == REPO_ROOT / "scripts" / "prodq.py", (
        f"the wrapper mounts {source}, not this checkout's copy"
    )
    assert ":ro" in mount_specs[0], f"prodq.py is mounted writable: {mount_specs[0]}"
    assert "example.invalid/app:test" in argv
    assert argv.index("example.invalid/app:test") < argv.index("SELECT 1"), (
        "the statement must come after the image, as an argument to the command"
    )


def test_extra_mounts_and_flags_come_from_the_environment(argv_recorder: Path):
    """Site-specific mounts are the operator's to supply, not ours to hardcode."""
    result = _run(["SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
        "VTS_PRODQ_MOUNTS": "/a/b:/app/b:ro /c/d:/app/d:ro",
        "VTS_PRODQ_ENGINE_ARGS": "--network=host",
    })
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert "/a/b:/app/b:ro" in argv
    assert "/c/d:/app/d:ro" in argv
    assert "--network=host" in argv


def test_write_and_json_flags_reach_the_script(argv_recorder: Path):
    """--write must survive the extra layer, or it silently becomes a read.

    Worse than an error: the caller believes the statement ran.
    """
    result = _run(["--write", "--json", "UPDATE tasks SET source_title = 'x'"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
    })
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert "--write" in argv
    assert "--json" in argv
    assert "UPDATE tasks SET source_title = 'x'" in argv


def test_sudo_is_opt_in_and_off_by_default(argv_recorder: Path):
    """Rootful podman needs sudo on some hosts and not on others.

    Off by default so the wrapper does not ask for a password where it is not
    needed — and so "it works on my machine" is a configuration, not a habit.
    """
    plain = _run(["SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
    })
    assert plain.returncode == 0, plain.stderr
    assert "sudo" not in plain.stdout

    # With it on, the recorder is invoked THROUGH sudo, so its own argv starts
    # with the engine. Asserting the composed command line instead.
    printed = _run(["--print", "SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(argv_recorder),
        "VTS_PRODQ_SUDO": "1",
    })
    assert printed.returncode == 0, printed.stderr
    assert "sudo" in printed.stdout


def test_print_shows_the_command_without_running_it(tmp_path: Path):
    """The wrapper has to be inspectable: it aims at production.

    `--print` is also how a reader learns what the environment assembled,
    without having to trust this file's description of it. Proven by a spy that
    leaves a FILE behind rather than by reading stdout — with --print the
    printed command contains the statement too, so stdout cannot tell "shown"
    from "run".
    """
    marker = tmp_path / "engine-ran"
    spy = tmp_path / "spy-engine"
    spy.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n', encoding="utf-8")
    spy.chmod(0o755)

    result = _run(["--print", "SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(spy),
    })
    assert result.returncode == 0, result.stderr
    assert "example.invalid/app:test" in result.stdout
    assert "SELECT 1" in result.stdout, "the command shown omits the statement"
    assert not marker.exists(), "--print executed the engine anyway"

    # And the control: without --print it does run, or the check above would
    # pass for a wrapper that never runs anything.
    ran = _run(["SELECT 1"], {
        "VTS_PRODQ_IMAGE": "example.invalid/app:test",
        "VTS_PRODQ_ENGINE": str(spy),
    })
    assert ran.returncode == 0, ran.stderr
    assert marker.exists(), "the wrapper never invoked the engine at all"


def test_the_wrapper_holds_no_real_infrastructure():
    """The public-repo rule, asserted rather than remembered.

    A host path, an image name or a service name in here is an infrastructure
    map for anyone reading the repository — and this is precisely the file where
    it would be convenient to write one.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    for needle in ("/opt/", "ghcr.io/", "docker.io/", ".fritz.box", "192.168.", "10.0."):
        assert needle not in text, f"{needle!r} appears in the wrapper"


def test_the_config_file_supplies_the_environment(tmp_path: Path, argv_recorder: Path):
    """The documented escape from "export five variables every session".

    This is the key the design depends on: real values live in a file on the
    host, never in git. Tested by running it, because a documented variable that
    nothing reads fails silently.
    """
    config = tmp_path / "prodq.env"
    config.write_text(
        'VTS_PRODQ_IMAGE="example.invalid/from-config:test"\n'
        'VTS_PRODQ_MOUNTS="/from/config:/app/config:ro"\n',
        encoding="utf-8",
    )
    result = _run(["SELECT 1"], {
        "VTS_PRODQ_CONFIG": str(config),
        "VTS_PRODQ_ENGINE": str(argv_recorder),
    })
    assert result.returncode == 0, result.stderr
    argv = result.stdout.splitlines()
    assert "example.invalid/from-config:test" in argv
    assert "/from/config:/app/config:ro" in argv
