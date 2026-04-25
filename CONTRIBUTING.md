# Contributing to vts

Thanks for your interest. vts is primarily a personal project, but
contributions — bug reports, fixes, ideas — are welcome.

## Quick links

- [LICENSE](LICENSE) — MIT.
- [SECURITY.md](SECURITY.md) — how to report security issues (please don't
  open public issues for those).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the system fits together.
- [docs/PROCESSING_CONTRACT.md](docs/PROCESSING_CONTRACT.md) — pipeline contract.
- [docs/LLM_BACKENDS.md](docs/LLM_BACKENDS.md) — supported LLM backends.

## Development setup

You need Python 3.14+, Docker (or Podman), and `ffmpeg` if you want to test
the worker locally without a container.

```bash
git clone https://github.com/<owner>/vts.git
cd vts
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

To run the full stack locally:

```bash
cp .env.example .env
docker compose --profile asr-whisper --profile llm-llamacpp up -d
```

See the [README](README.md) Quick Start for the model-download step.

## Running tests

```bash
python -m pytest -q
```

There is no separate lint config yet; the codebase generally follows
`ruff format` defaults.

## Submitting changes

1. **Open an issue first** for non-trivial changes so we can agree on
   direction before you spend time on a PR.
2. **One logical change per PR.** Mixed-concern PRs are hard to review.
3. **Tests.** Add or update tests when fixing a bug or adding behavior.
4. **Version bump.** This project bumps the patch version on every commit
   (`python scripts/bump_version.py patch`). Bots that bump versions on
   merge are not in place — please bump in your PR.
5. **Commit message.** Follow the [conventional-commit](https://www.conventionalcommits.org/)
   style: `feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `ci:`, `build:`,
   `chore:`, `test:`. The first line is a short summary; the body explains
   *why*. Release notes are auto-generated from these prefixes by
   `git-cliff` on every `build-X.Y.Z` tag — non-conventional commits are
   silently dropped from the changelog.

## Code style

- Type hints on all new code.
- `from __future__ import annotations` at the top of new modules.
- Async I/O — vts is built around `asyncio` and `httpx.AsyncClient`. Avoid
  blocking calls in the event loop.
- Comments explain *why*, not *what*. Function names should make *what*
  obvious.

## Reviewing your own PR

Before requesting review, run through this:

- [ ] Tests pass: `python -m pytest -q`.
- [ ] Patch version bumped.
- [ ] No new hardcoded paths, secrets, or personal URLs.
- [ ] If you added a new config key: documented in [README](README.md) or
      [config.yaml](config.yaml).
- [ ] If you added a new pipeline stage: emit metrics for it (see
      [docs/PROCESSING_CONTRACT.md](docs/PROCESSING_CONTRACT.md)).

## Internal automation conventions

This repo uses several layered conventions for AI-assisted development:

- [PROJECT_RULES.md](PROJECT_RULES.md) — workflow contract.
- [CLAUDE.md](CLAUDE.md), [CODEX.md](CODEX.md), [AGENTS.md](AGENTS.md) — agent
  entry points (managed by an internal-standards toolchain).
- `.beads/` — local issue tracker (gitignored).

These are kept in the repo so you can see how the project is actually
maintained. They are not requirements for outside contributors.
