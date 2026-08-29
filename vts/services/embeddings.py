"""Text embeddings for corpus indexing (vts-twe7 / VOS-131).

Served by the SAME gateway as the chat model — only the model name differs — so
this adds no sidecar and no new deployment surface, which is what "fully
compatible with a private/self-hosted deployment" asks for.

Measured against the deployment before this was written: bge-m3 returns 1024
dimensions, its vectors are normalised (norm 1.0000, so a dot product IS the
cosine), and a batch of 32 takes 1.49s — about 21 texts a second. A whole
recording therefore indexes in seconds, which is why indexing does not need to
be scheduled around transcription despite sharing the GPU.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """The gateway did not return a usable set of embeddings."""


class EmbeddingClient:
    """Minimal OpenAI-compatible /embeddings client.

    Deliberately strict about partial results: a batch that comes back short,
    or out of order, would attach vectors to the WRONG chunks — a corruption
    that no amount of downstream checking would notice, because every chunk
    would still have a plausible vector. So anything unexpected raises.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: int = 120,
        batch_size: int = DEFAULT_BATCH_SIZE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._batch_size = max(1, int(batch_size))
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # A self-hosted gateway may not want one at all, and "Bearer None"
        # fails against a server that validates what it was handed.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings for `texts`, in the same order."""
        if not texts:
            return []
        out: list[list[float]] = []
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport
        ) as client:
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start:start + self._batch_size]
                out.extend(await self._embed_batch(client, batch))
        return out

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        try:
            response = await client.post(
                self._url,
                headers=self._headers(),
                json={"model": self._model, "input": batch},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"embedding gateway returned {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise EmbeddingError("embedding response was not JSON") from exc

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise EmbeddingError("embedding response carried no data list")

        # Ordered by the documented `index` rather than by arrival: the API
        # does not promise arrival order, and trusting it would silently pair
        # each vector with the wrong text.
        vectors: dict[int, list[float]] = {}
        for position, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector:
                continue
            index = row.get("index")
            vectors[int(index) if isinstance(index, int) else position] = [
                float(value) for value in vector
            ]

        if len(vectors) != len(batch) or set(vectors) != set(range(len(batch))):
            raise EmbeddingError(
                f"embedding gateway returned {len(vectors)} usable vectors "
                f"for {len(batch)} inputs"
            )
        return [vectors[i] for i in range(len(batch))]
