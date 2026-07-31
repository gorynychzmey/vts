from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from vts.delivery.contract import (
    DeliveryAdapter,
    DeliveryError,
    DeliveryPayload,
    DeliveryTargetConfig,
    TaskMeta,
)
from vts_outline import OutlineAdapter

BASE = "https://outline.example/api"


def _payload(task_id: str = "t-1") -> DeliveryPayload:
    meta = TaskMeta(
        source_url="http://v/1",
        source_title="Meeting",
        language="en",
        duration_s=60.0,
        created_at=datetime.now(timezone.utc),
    )
    return DeliveryPayload(
        task_id=task_id,
        variant="summary",
        content="# Notes\nbody",
        content_format="markdown",
        task=meta,
    )


def _target(**config) -> DeliveryTargetConfig:
    cfg = {"base_url": BASE, "collection_id": "c1"}
    cfg.update(config)
    return DeliveryTargetConfig(config=cfg, secrets={"api_token": "tok"})


class _Recorder:
    """Collects the requests an httpx.MockTransport sees."""

    def __init__(self, routes: dict[str, httpx.Response]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        self.headers = dict(request.headers)
        if path not in self.routes:
            raise AssertionError(f"unexpected call to {path}")
        return self.routes[path]

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def body_for(self, path: str) -> dict:
        for p, b in self.calls:
            if p == path:
                return b
        raise AssertionError(f"{path} was never called")


def test_satisfies_delivery_adapter_protocol():
    assert isinstance(OutlineAdapter(), DeliveryAdapter)


def test_metadata():
    adapter = OutlineAdapter()
    assert adapter.name == "outline"
    assert adapter.secret_keys() == ["api_token"]
    schema = adapter.config_schema()
    assert schema["required"] == ["base_url", "collection_id"]
    assert set(schema["properties"]) == {
        "base_url",
        "collection_id",
        "default_variant",
    }
    assert schema["properties"]["default_variant"]["enum"] == [
        "raw",
        "redacted",
        "summary",
    ]


@pytest.mark.asyncio
async def test_creates_document_when_search_finds_nothing():
    rec = _Recorder(
        {
            "/api/documents.search": httpx.Response(200, json={"data": []}),
            "/api/documents.create": httpx.Response(
                200, json={"data": {"id": "doc1", "url": "/doc/meeting-doc1"}}
            ),
        }
    )
    adapter = OutlineAdapter(transport=rec.transport())

    result = await adapter.deliver(_payload(), _target())

    assert rec.paths == ["/api/documents.search", "/api/documents.create"]
    assert "/api/documents.update" not in rec.paths
    assert result.external_id == "doc1"
    assert result.external_url == "https://outline.example/doc/meeting-doc1"

    created = rec.body_for("/api/documents.create")
    assert created["collectionId"] == "c1"
    assert created["title"] == "Meeting"
    assert created["publish"] is True
    assert "<!-- vts-task:t-1 -->" in created["text"]
    assert "http://v/1" in created["text"]
    assert "lang: en" in created["text"]
    assert "# Notes\nbody" in created["text"]
    assert rec.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_updates_existing_document_when_search_hits():
    """A hit IS the match — verified against a real Outline response shape.

    documents.search returns metadata + a truncated `context` snippet peppered
    with <b> highlight tags, and NO `text` field. Matching on the body would
    therefore never fire, and every retry would create a duplicate. The marker
    carries the task UUID, so a hit on it is conclusive.
    """
    rec = _Recorder(
        {
            "/api/documents.search": httpx.Response(
                200,
                json={
                    "data": [
                        {
                            # Real shape: no "text", context is a highlighted snippet
                            # that can even split the marker across <b> tags.
                            "context": "source: http\n<b>vts-task</b>:t-1 ...",
                            "document": {
                                "id": "doc9",
                                "url": "/doc/meeting-doc9",
                                "title": "Meeting",
                            },
                        }
                    ]
                },
            ),
            "/api/documents.update": httpx.Response(
                200, json={"data": {"id": "doc9", "url": "/doc/meeting-doc9"}}
            ),
        }
    )
    adapter = OutlineAdapter(transport=rec.transport())

    result = await adapter.deliver(_payload(), _target())

    assert "/api/documents.update" in rec.paths
    assert "/api/documents.create" not in rec.paths, "must not duplicate on retry"
    updated = rec.body_for("/api/documents.update")
    assert updated["id"] == "doc9"
    assert updated["title"] == "Meeting"
    assert "<!-- vts-task:t-1 -->" in updated["text"]
    assert result.external_id == "doc9"
    assert result.external_url == "https://outline.example/doc/meeting-doc9"


@pytest.mark.asyncio
async def test_search_queries_the_task_marker():
    """The uniqueness of the query is what makes a bare hit safe to trust."""
    rec = _Recorder(
        {
            "/api/documents.search": httpx.Response(200, json={"data": []}),
            "/api/documents.create": httpx.Response(
                200, json={"data": {"id": "doc2", "url": "/doc/doc2"}}
            ),
        }
    )
    adapter = OutlineAdapter(transport=rec.transport())

    await adapter.deliver(_payload(), _target())

    assert rec.body_for("/api/documents.search")["query"] == "vts-task:t-1"


@pytest.mark.asyncio
async def test_search_http_500_raises_delivery_error():
    rec = _Recorder({"/api/documents.search": httpx.Response(500, text="boom")})
    adapter = OutlineAdapter(transport=rec.transport())

    with pytest.raises(DeliveryError):
        await adapter.deliver(_payload(), _target())


@pytest.mark.asyncio
async def test_create_http_500_raises_delivery_error():
    rec = _Recorder(
        {
            "/api/documents.search": httpx.Response(200, json={"data": []}),
            "/api/documents.create": httpx.Response(500, text="boom"),
        }
    )
    adapter = OutlineAdapter(transport=rec.transport())

    with pytest.raises(DeliveryError):
        await adapter.deliver(_payload(), _target())


@pytest.mark.asyncio
async def test_transport_error_raises_delivery_error():
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    adapter = OutlineAdapter(transport=httpx.MockTransport(_boom))

    with pytest.raises(DeliveryError):
        await adapter.deliver(_payload(), _target())


@pytest.mark.asyncio
async def test_non_markdown_content_is_fenced_and_title_falls_back_to_url():
    rec = _Recorder(
        {
            "/api/documents.search": httpx.Response(200, json={"data": []}),
            "/api/documents.create": httpx.Response(
                200, json={"data": {"id": "doc3", "url": "/doc/doc3"}}
            ),
        }
    )
    meta = TaskMeta(
        source_url="http://v/2",
        source_title=None,
        language=None,
        duration_s=None,
        created_at=datetime.now(timezone.utc),
    )
    payload = DeliveryPayload(
        task_id="t-2",
        variant="raw",
        content="plain transcript",
        content_format="txt",
        task=meta,
    )
    adapter = OutlineAdapter(transport=rec.transport())

    await adapter.deliver(payload, _target())

    created = rec.body_for("/api/documents.create")
    assert created["title"] == "http://v/2"
    assert "```\nplain transcript\n```" in created["text"]
    assert "lang:" not in created["text"]


@pytest.mark.asyncio
async def test_absolute_base_url_without_api_suffix():
    rec = _Recorder(
        {
            "/documents.search": httpx.Response(200, json={"data": []}),
            "/documents.create": httpx.Response(
                200, json={"data": {"id": "doc4", "url": "/doc/doc4"}}
            ),
        }
    )
    adapter = OutlineAdapter(transport=rec.transport())

    result = await adapter.deliver(
        _payload(), _target(base_url="https://outline.example/")
    )

    assert result.external_url == "https://outline.example/doc/doc4"
