# VTS Project Memory

- Python project: `vts/` package, FastAPI + SQLAlchemy + Redis
- Tests: run locally via `.venv` before commit; also run in Docker container during GitHub build
- Local test setup (once): `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
- Version: `vts/__init__.py` · bump: `python3 scripts/bump_version.py patch`

## Git Workflow (ALWAYS FOLLOW → see [git_workflow.md](git_workflow.md))

Every completed task: **bump patch → prepare_commit.sh → commit → push**
On `build` request: additionally tag `build-X.Y.Z` and push tag

Use `Agent(subagent_type="general-purpose", model="haiku")` for mechanical git steps.

## Project Notes

- All LLM summaries (segment + final) return plain markdown, not JSON (`use_json_format=False`)
- Segment summaries stored as raw strings in `window_*.txt` and `windows.json`
- Final summary written directly to `final.md`; `final.json` stores `{"raw": <text>}` for checkpoint
