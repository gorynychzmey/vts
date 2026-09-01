"""Regressions from the 2026-09-01 review of 1.7.86 (vts-dp8d, hg1k, ohl4, mlvt).

Every one of these shipped. They share a shape worth naming: the code was
verified by *building the server* and by tests that never called the failing
path, so a wrong constructor and a missing import both passed CI.
"""
from __future__ import annotations

import uuid

import pytest


def test_prompt_result_is_built_with_the_fields_the_schema_has():
    """vts-dp8d: get_recording_prompt_result could not return on ANY path.

    It passed ref/title/text; PromptResult declares task_id/source/id/content.
    Constructing it raised ValidationError every time.
    """
    from vts.mcp.schemas import PromptResult

    import ast

    # Assert against the CALL SITE, not just the schema: the bug was that the
    # tool named fields the schema does not have, and a schema-only test
    # passes right through that.
    src = open("vts/mcp/tools_registry/recordings.py").read()
    declared = set(PromptResult.model_fields)
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PromptResult"
        ):
            passed = {kw.arg for kw in node.keywords if kw.arg}
            unknown = passed - declared
            missing = declared - passed
            assert not unknown, f"PromptResult(...) passes unknown fields: {unknown}"
            assert not missing, f"PromptResult(...) omits required fields: {missing}"


def test_tasks_registry_imports_every_name_it_raises():
    """vts-hg1k: HTTPException was used but never imported -> NameError, not 404."""
    import ast

    src = open("vts/mcp/tools_registry/tasks.py").read()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update((a.asname or a.name).split(".")[0] for a in node.names)
    raised = {
        node.exc.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)          # not a bare `raise`
        and isinstance(node.exc.func, ast.Name)     # not `mod.Error(...)`
    }
    builtins = {"ValueError", "RuntimeError", "TypeError", "KeyError"}
    missing = raised - imported - builtins
    assert not missing, f"raised but not imported: {missing}"


@pytest.mark.asyncio
async def test_task_page_reports_a_real_total(fake_repo_with_tasks):
    """vts-ohl4: TaskPage.total stayed 0 on a non-empty page.

    The field exists so a client can tell a first page from the whole answer.
    Left at 0 it says "nothing here" while handing over rows — worse than
    absent, because it looks authoritative.
    """
    from vts.mcp.tools import list_tasks

    repo, user = fake_repo_with_tasks
    page = await list_tasks(user=user, repo=repo, limit=2)
    assert len(page.tasks) == 2
    assert page.total == 5, f"total={page.total} with 5 tasks in the repo"


@pytest.mark.asyncio
async def test_offset_beyond_the_candidate_ceiling_is_reported_honestly():
    """vts-mlvt: past _MAX_FETCH candidates every page is empty.

    `total` promises more while `offset` can never reach it, which is the same
    silent lie `total` was introduced to remove — just moved one step along.
    """
    from vts.services import corpus_search

    assert corpus_search._MAX_FETCH == 500
    # An offset the fetch budget cannot serve must not be accepted quietly.
    with pytest.raises(ValueError, match="offset"):
        corpus_search._check_offset_reachable(offset=600, limit=10)
    corpus_search._check_offset_reachable(offset=100, limit=10)  # fine


@pytest.mark.asyncio
async def test_delete_task_asks_before_removing_shared_artifacts(monkeypatch):
    """vts-lyii: the MCP delete removed the directory unconditionally.

    A recording detached from this task can still own the same directory —
    that is what SET NULL is for. The HTTP path checks; the tool did not, so
    going through MCP was the cheaper way to lose someone's files.
    """
    import vts.mcp.tools_registry.tasks as mod

    assert "artifacts_removable_for_task" in open(mod.__file__).read(), (
        "the guard is not even referenced"
    )
    import ast

    tree = ast.parse(open(mod.__file__).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_delete_task":
            body = ast.dump(node)
            assert "artifacts_removable_for_task" in body, (
                "_delete_task does not consult the guard"
            )
            # And the guard must be consulted BEFORE the rows go, or the
            # claims it inspects are already deleted.
            src = ast.unparse(node)
            guard = src.index("artifacts_removable_for_task")
            delete = src.index("delete_task_with_recording")
            assert guard < delete, "guard runs after the rows are deleted"
            return
    raise AssertionError("_delete_task not found")
