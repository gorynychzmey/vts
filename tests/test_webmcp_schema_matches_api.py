"""A WebMCP tool must not advertise a parameter the endpoint throws away.

vts-a2ww: `submit_video` in `vts/static/webmcp.js` offered `preset` and posted
the input straight to `/api/tasks`. `TaskCreateRequest` has no such field and
the module sets no `model_config`, so pydantic's default `extra="ignore"` drops
the key without a word. The browser agent got a 200 and a task id, reported
"submitted with your Meeting preset", and the task ran on bare defaults — no
diarize, no delivery, the system summary. Nothing anywhere said otherwise.

Presets are not a thing this endpoint has ever understood: the web form resolves
one into the form controls and posts the EXPANDED values (app.js, the payload
built around line 4214). The server-side MCP `submit_video` does take `preset`
and expands it itself, which is why the two surfaces drifted while sharing a
tool name.

So the invariant is the one that matters to a caller: everything the tool SAYS
it takes must be something the endpoint actually reads. It catches the next
drift in either direction — a property added to the tool, or a field renamed in
the schema — which a test pinned to the word "preset" would not.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from vts.api.schemas import TaskCreateRequest

WEBMCP = Path(__file__).resolve().parent.parent / "vts" / "static" / "webmcp.js"


def _advertised_properties(tool: str) -> set[str]:
    """Property names in `tool`'s inputSchema, read out of the source.

    Deliberately strict: if the shape ever stops matching, this raises instead
    of returning an empty set, because a silently empty set would make every
    assertion below pass.
    """
    source = WEBMCP.read_text(encoding="utf-8")
    # Comments first: prose inside the schema reads as a property to the scan
    # below ("understood one: the web form…" yielded a property named `one`).
    # Safe here because no string literal in this file contains `//`.
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
    start = source.find(f'name: "{tool}"')
    assert start != -1, f"{tool} is not declared in {WEBMCP.name}"

    marker = source.index("properties: {", start)
    depth, end = 0, None
    for index in range(marker + len("properties: "), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    assert end is not None, f"unbalanced inputSchema braces for {tool}"
    body = source[marker + len("properties: ") + 1 : end]

    # Keys at depth zero of the properties object; nested objects (a property's
    # own `type`/`items`) must not be mistaken for properties.
    names, depth = set(), 0
    for key_match in re.finditer(r"[{}]|(\w+)\s*:", body):
        token = key_match.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0 and key_match.group(1):
            names.add(key_match.group(1))
    assert names, f"parsed no properties for {tool}; the regex no longer matches"
    return names


def _accepted_fields() -> set[str]:
    names: set[str] = set()
    for field_name, field in TaskCreateRequest.model_fields.items():
        names.add(field_name)
        alias = field.validation_alias
        if alias is None:
            continue
        # AliasChoices keeps its options in `choices`; a plain string alias is
        # itself the accepted name.
        names.update(getattr(alias, "choices", [alias]))
    return {n for n in names if isinstance(n, str)}


def test_submit_video_advertises_only_fields_the_endpoint_reads():
    advertised = _advertised_properties("submit_video")
    accepted = _accepted_fields()

    ignored = advertised - accepted
    assert not ignored, (
        "submit_video offers "
        + ", ".join(sorted(ignored))
        + " but POST /api/tasks has no such field and pydantic drops it in"
        " silence, so the caller is told the request succeeded as asked."
        f" Accepted: {', '.join(sorted(accepted))}"
    )


def test_the_schema_is_really_being_read():
    """Guard against the parser going quietly blind.

    Both assertions above are satisfied by an empty `advertised` set, so the
    parse itself needs a witness: `url` is required by the tool and by the
    endpoint, and if it stops being found the extraction broke rather than the
    code improving.
    """
    assert "url" in _advertised_properties("submit_video")
    assert "url" in _accepted_fields()
