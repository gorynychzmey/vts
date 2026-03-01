# VTS Project Memory

- Python project: `vts/` package, FastAPI + SQLAlchemy + Redis
- Tests run **inside Docker container** — no local pytest
- Version: `vts/__init__.py` · bump: `python scripts/bump_version.py patch`

## Git Workflow (ALWAYS FOLLOW → see [git_workflow.md](git_workflow.md))

Every completed task: **bump patch → prepare_commit.sh → commit → push**
On `build` request: additionally tag `build-X.Y.Z` and push tag

Use `Agent(subagent_type="general-purpose", model="haiku")` for mechanical git steps.

## Project Notes

- Segment prompt: plain structured text, no JSON (`prompts/segment_prompt.md`)
- Final summary: JSON output (`prompts/global_prompt.md`)
- `_extract_window_text()` in `processor.py` — extracts only `summary` text (not `raw`) for final LLM call
