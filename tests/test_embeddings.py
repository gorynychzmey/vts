"""The embedding client for corpus indexing (vts-twe7 / VOS-131).

Embeddings are served by the SAME gateway as the chat model — only the model
name differs — so this adds no sidecar and no new deployment surface, which is
what "fully compatible with a private/self-hosted deployment" asks for.

Measured against the real gateway before this was written: bge-m3 returns 1024
dimensions, vectors are normalised (norm 1.0000), and a batch of 32 takes 1.49s
(~21 texts/s). So a whole recording indexes in seconds, and indexing does not
have to be scheduled around transcription after all.
"""
from __future__ import annotations

import httpx
import pytest

from vts.services.embeddings import EmbeddingClient, EmbeddingError


def _client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return EmbeddingClient(
        url="http://gateway/v1", api_key="k", model="bge-m3",
        transport=transport, **kwargs,
    )


@pytest.mark.asyncio
async def test_embeds_a_batch_in_order():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [0.1, 0.2]},
            {"index": 1, "embedding": [0.3, 0.4]},
        ]})

    out = await _client(handler).embed(["first", "second"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["body"]["model"] == "bge-m3"
    assert seen["body"]["input"] == ["first", "second"]
    assert seen["auth"] == "Bearer k"


@pytest.mark.asyncio
async def test_results_are_reordered_by_index_not_trusted_in_arrival_order():
    # The API documents an `index` field precisely because order is not
    # guaranteed. Trusting arrival order would silently attach each embedding
    # to the wrong chunk — a corruption no test of "did it return 2 vectors"
    # would catch.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [9.0]},
            {"index": 0, "embedding": [1.0]},
        ]})

    assert await _client(handler).embed(["a", "b"]) == [[1.0], [9.0]]


@pytest.mark.asyncio
async def test_large_input_is_split_into_batches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        calls.append(len(body["input"]))
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [float(i)]} for i in range(len(body["input"]))
        ]})

    client = _client(handler, batch_size=3)
    out = await client.embed([f"t{i}" for i in range(7)])
    assert len(out) == 7
    assert calls == [3, 3, 1], f"unexpected batching: {calls}"


@pytest.mark.asyncio
async def test_empty_input_makes_no_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a request was made for an empty input")

    assert await _client(handler).embed([]) == []


@pytest.mark.asyncio
async def test_a_gateway_error_raises_rather_than_returning_partial_results():
    # Half a batch of embeddings is worse than none: the caller would store
    # chunks whose vectors belong to other chunks.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream on fire")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a"])


@pytest.mark.asyncio
async def test_a_short_batch_response_is_an_error_not_a_silent_gap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a", "b"])


@pytest.mark.asyncio
async def test_a_malformed_payload_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not-data": []})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a"])


@pytest.mark.asyncio
async def test_no_api_key_sends_no_authorization_header():
    # A self-hosted gateway may not require one; sending "Bearer None" would
    # fail against a server that validates the header it was given.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = EmbeddingClient(
        url="http://gateway/v1", api_key=None, model="bge-m3",
        transport=httpx.MockTransport(handler),
    )
    await client.embed(["a"])
    assert seen["auth"] is None
