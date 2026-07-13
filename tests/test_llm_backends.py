"""Backend detection + context-window discovery (vts-tel).

discover_n_ctx probes the configured LLM URL to identify the backend
(LiteLLM -> Ollama -> llama-server, in that order) and asks the matched
backend for the model's context window. When nothing matches, or the matched
backend cannot answer, the generic fallback constant applies.

Tests drive the real discovery code over httpx.MockTransport — no network.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from vts.services.llm_backends import discover_n_ctx

BASE_URL = "http://llm.local:4000/v1"
FALLBACK = 12345


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _discover(handler, *, model: str = "qwen3.6:35b", api_key: str | None = "sk-test"):
    return asyncio.run(
        discover_n_ctx(
            url=BASE_URL,
            api_key=api_key,
            model=model,
            fallback_n_ctx=FALLBACK,
            transport=_transport(handler),
        )
    )


def test_litellm_backend_detected_via_model_info():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            assert request.headers.get("authorization") == "Bearer sk-test"
            return httpx.Response(200, json={"data": [
                {"model_name": "other", "model_info": {"max_input_tokens": 999}},
                {"model_name": "qwen3.6:35b", "model_info": {"max_input_tokens": 114688}},
            ]})
        return httpx.Response(404)

    backend, n_ctx = _discover(handler)
    assert backend == "litellm"
    assert n_ctx == 114688


def test_litellm_detected_but_unknown_size_falls_back_to_constant():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": [
                {"model_name": "qwen3.6:35b", "model_info": {"max_input_tokens": None}},
            ]})
        return httpx.Response(404)

    backend, n_ctx = _discover(handler)
    assert backend == "litellm"
    assert n_ctx == FALLBACK


def test_ollama_backend_num_ctx_from_show_parameters():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(404)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.20.0"})
        if request.url.path == "/api/show" and request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == "qwen3.6:35b-a3b-112k"
            return httpx.Response(200, json={
                "parameters": "num_ctx                        114688\ntemperature                    1",
                "model_info": {"qwen35moe.context_length": 262144},
            })
        return httpx.Response(404)

    backend, n_ctx = _discover(handler, model="qwen3.6:35b-a3b-112k")
    assert backend == "ollama"
    assert n_ctx == 114688


def test_ollama_without_num_ctx_parameter_falls_back_to_constant():
    """Native context_length is deliberately NOT trusted: the server may
    allocate far less than the model supports (VRAM-tier default)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model/info":
            return httpx.Response(404)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.20.0"})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={
                "parameters": "temperature 1",
                "model_info": {"qwen35moe.context_length": 262144},
            })
        return httpx.Response(404)

    backend, n_ctx = _discover(handler)
    assert backend == "ollama"
    assert n_ctx == FALLBACK


def test_llama_server_backend_n_ctx_from_props():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/model/info", "/api/version"):
            return httpx.Response(404)
        if request.url.path == "/props":
            return httpx.Response(200, json={"n_ctx": 32768, "model_path": "/m.gguf"})
        return httpx.Response(404)

    backend, n_ctx = _discover(handler)
    assert backend == "llama-server"
    assert n_ctx == 32768


def test_generic_fallback_when_nothing_matches():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    backend, n_ctx = _discover(handler)
    assert backend == "generic"
    assert n_ctx == FALLBACK


def test_generic_fallback_on_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    backend, n_ctx = _discover(handler)
    assert backend == "generic"
    assert n_ctx == FALLBACK


def test_summary_n_ctx_setting_exists_and_yaml_maps():
    from vts.core.config import Settings, _normalize_yaml_overrides

    assert Settings(database_url="sqlite+aiosqlite://").summary_n_ctx == 32768
    normalized = _normalize_yaml_overrides({"summary": {"n_ctx": 40960}})
    assert normalized["summary_n_ctx"] == 40960


def test_processor_get_n_ctx_uses_discovery(monkeypatch):
    import logging
    import uuid
    from types import SimpleNamespace

    from vts.pipeline.processor import TaskProcessor

    proc = TaskProcessor.__new__(TaskProcessor)
    proc.settings = SimpleNamespace(
        llm_url="http://llm.local:4000/v1",
        llm_model="qwen3.6:35b",
        llm_api_key="sk-test",
        summary_n_ctx=32768,
    )
    proc._task_n_ctx = {}

    async def fake_discover(**kwargs):
        assert kwargs["url"] == "http://llm.local:4000/v1"
        assert kwargs["model"] == "qwen3.6:35b"
        assert kwargs["fallback_n_ctx"] == 32768
        return ("litellm", 114688)

    monkeypatch.setattr("vts.pipeline.processor.discover_n_ctx", fake_discover)
    task_id = uuid.uuid4()
    logger = logging.getLogger("test_llm_backends")
    n_ctx = asyncio.run(proc._get_n_ctx(task_id, logger))
    assert n_ctx == 114688
    assert proc._task_n_ctx[str(task_id)] == 114688
