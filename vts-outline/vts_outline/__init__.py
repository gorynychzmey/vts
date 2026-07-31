"""Outline delivery adapter for VTS.

Registered with the VTS core via the ``vts.delivery`` entry point group::

    [project.entry-points."vts.delivery"]
    outline = "vts_outline:OutlineAdapter"

The adapter is stateless. Delivery is at-least-once, so ``deliver()`` is made
idempotent by embedding a hidden marker ``<!-- vts-task:{task_id} -->`` in the
document body and searching for it before creating a new document.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from vts.delivery.contract import (
    DeliveryError,
    DeliveryPayload,
    DeliveryResult,
    DeliveryTargetConfig,
)

__all__ = ["OutlineAdapter"]

_MARKER_PREFIX = "vts-task:"


class OutlineAdapter:
    """Delivers a VTS transcript/summary into an Outline collection."""

    name = "outline"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        # Both hooks exist for tests only; production constructs the adapter
        # with no arguments (entry points instantiate it as ``OutlineAdapter()``)
        # and the real ``httpx.AsyncClient`` is used.
        self._transport = transport
        self._client_factory = client_factory

    # -- contract ---------------------------------------------------------

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "base_url": {
                    "type": "string",
                    "description": "Outline API base URL, e.g. https://outline.example/api",
                },
                "collection_id": {
                    "type": "string",
                    "description": "Outline collection id new documents are created in",
                },
                "default_variant": {
                    "type": "string",
                    "enum": ["raw", "redacted", "summary"],
                    "description": "Variant used when the task does not specify one",
                },
            },
            "required": ["base_url", "collection_id"],
        }

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    # -- rendering --------------------------------------------------------

    def _title(self, payload: DeliveryPayload) -> str:
        return payload.task.source_title or payload.task.source_url

    def _body(self, payload: DeliveryPayload) -> str:
        header = f"> source: {payload.task.source_url}"
        if payload.task.language:
            header += f" · lang: {payload.task.language}"
        content = payload.content
        if payload.content_format != "markdown":
            content = f"```\n{content}\n```"
        marker = f"\n\n<!-- {_MARKER_PREFIX}{payload.task_id} -->"
        return f"{header}\n\n{content}{marker}"

    # -- delivery ---------------------------------------------------------

    def _make_client(self, headers: dict[str, str]) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(headers=headers)
        if self._transport is not None:
            return httpx.AsyncClient(
                timeout=30, headers=headers, transport=self._transport
            )
        return httpx.AsyncClient(timeout=30, headers=headers)

    async def deliver(
        self, payload: DeliveryPayload, target: DeliveryTargetConfig
    ) -> DeliveryResult:
        base = str(target.config["base_url"]).rstrip("/")
        collection_id = target.config["collection_id"]
        token = target.secrets.get("api_token", "")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        marker = f"{_MARKER_PREFIX}{payload.task_id}"

        try:
            async with self._make_client(headers) as client:
                existing_id = await self._find_existing(client, base, marker)

                if existing_id:
                    resp = await client.post(
                        f"{base}/documents.update",
                        json={
                            "id": existing_id,
                            "title": self._title(payload),
                            "text": self._body(payload),
                        },
                    )
                else:
                    resp = await client.post(
                        f"{base}/documents.create",
                        json={
                            "collectionId": collection_id,
                            "title": self._title(payload),
                            "text": self._body(payload),
                            "publish": True,
                        },
                    )
                if resp.status_code >= 400:
                    raise DeliveryError(
                        f"Outline write failed: {resp.status_code} {resp.text}"
                    )
                doc = self._data(resp)
                return DeliveryResult(
                    external_id=doc.get("id"),
                    external_url=self._absolute_url(base, doc.get("url")),
                )
        except httpx.HTTPError as exc:  # transport/timeout failures are retryable
            raise DeliveryError(f"Outline HTTP error: {exc}") from exc

    async def _find_existing(
        self, client: httpx.AsyncClient, base: str, marker: str
    ) -> str | None:
        """Return the id of a document already carrying this task's marker.

        A search HIT is itself the match. Outline's documents.search indexes the
        full document body but does NOT return it: each hit carries metadata plus
        a `context` snippet, and that snippet is truncated and littered with <b>
        highlight tags, so re-checking `marker in text` would fail on a document
        that genuinely contains the marker — and the adapter would create a
        duplicate on every retry, breaking the at-least-once guarantee.

        Relying on the hit alone is safe because the query is
        `vts-task:<task_id>` with a UUID in it: nothing else matches it.
        """
        found = await client.post(f"{base}/documents.search", json={"query": marker})
        if found.status_code >= 400:
            raise DeliveryError(
                f"Outline search failed: {found.status_code} {found.text}"
            )
        data = self._data(found, default=[])
        if not isinstance(data, list):
            return None
        for hit in data:
            if not isinstance(hit, dict):
                continue
            doc = hit.get("document") or hit
            doc_id = doc.get("id") if isinstance(doc, dict) else None
            if doc_id:
                return str(doc_id)
        return None

    @staticmethod
    def _data(resp: httpx.Response, default: Any = None) -> Any:
        try:
            body = resp.json()
        except ValueError as exc:
            raise DeliveryError(f"Outline returned non-JSON response: {exc}") from exc
        if not isinstance(body, dict):
            raise DeliveryError("Outline returned an unexpected response shape")
        value = body.get("data")
        if value is None:
            return {} if default is None else default
        return value

    @staticmethod
    def _absolute_url(base: str, url: str | None) -> str | None:
        """Outline returns a relative url like ``/doc/slug``; make it absolute."""
        if not url:
            return None
        if not url.startswith("/"):
            return url
        site_root = base[: -len("/api")] if base.endswith("/api") else base
        return f"{site_root}{url}"
