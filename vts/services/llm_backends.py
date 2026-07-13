"""LLM backend detection and context-window discovery (vts-tel).

The worker talks to its LLM through a single OpenAI-compatible URL, but the
server behind it varies (LiteLLM proxy, Ollama, plain llama-server). Each
backend exposes the model's usable context window differently, so discovery
is modeled as an ordered list of backend classes: every class implements
``probe()`` (unambiguously "is this you?") and ``get_n_ctx(model)``. The
first backend whose probe matches answers; if none match — or the matched
backend cannot answer for this model — the configured constant applies
(``summary_n_ctx``, surfaced here as ``fallback_n_ctx``).

Adding a backend = one subclass + one entry in ``_BACKENDS``.
"""
from __future__ import annotations

import json
import re

import httpx


def _base_root(url: str) -> str:
    """Server root for a configured LLM URL (strips a trailing /v1)."""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url[: -len("/v1")]
    return url


def _json_or_none(response: httpx.Response) -> dict | None:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class LLMBackend:
    """One supported LLM server type behind the configured URL."""

    name = "generic"

    async def probe(self, client: httpx.AsyncClient, root: str) -> bool:
        raise NotImplementedError

    async def get_n_ctx(
        self, client: httpx.AsyncClient, root: str, model: str
    ) -> int | None:
        raise NotImplementedError


class LiteLLMBackend(LLMBackend):
    """LiteLLM proxy: /model/info carries per-model max_input_tokens."""

    name = "litellm"

    async def _model_info(self, client: httpx.AsyncClient, root: str) -> dict | None:
        response = await client.get(f"{root}/model/info")
        if "x-litellm-version" not in response.headers and not response.is_success:
            return None
        payload = _json_or_none(response)
        if payload is None or not isinstance(payload.get("data"), list):
            # A litellm header on a non-JSON reply still identifies the
            # backend; report detection with no data.
            return {"data": []} if "x-litellm-version" in response.headers else None
        return payload

    async def probe(self, client: httpx.AsyncClient, root: str) -> bool:
        return await self._model_info(client, root) is not None

    async def get_n_ctx(
        self, client: httpx.AsyncClient, root: str, model: str
    ) -> int | None:
        payload = await self._model_info(client, root)
        if payload is None:
            return None
        for entry in payload.get("data", []):
            if not isinstance(entry, dict) or entry.get("model_name") != model:
                continue
            info = entry.get("model_info")
            value = info.get("max_input_tokens") if isinstance(info, dict) else None
            if isinstance(value, int) and value > 0:
                return value
        return None


class OllamaBackend(LLMBackend):
    """Ollama: /api/version identifies it; /api/show carries num_ctx.

    Only an explicit ``num_ctx`` model parameter is trusted. The native
    ``context_length`` is deliberately ignored: Ollama may allocate far less
    than the model supports (VRAM-tier server default), so advertising the
    native value would overstate the usable window.
    """

    name = "ollama"

    async def probe(self, client: httpx.AsyncClient, root: str) -> bool:
        response = await client.get(f"{root}/api/version")
        if not response.is_success:
            return False
        payload = _json_or_none(response)
        return payload is not None and "version" in payload

    async def get_n_ctx(
        self, client: httpx.AsyncClient, root: str, model: str
    ) -> int | None:
        response = await client.post(f"{root}/api/show", json={"model": model})
        if not response.is_success:
            return None
        payload = _json_or_none(response)
        parameters = payload.get("parameters") if payload else None
        if not isinstance(parameters, str):
            return None
        match = re.search(r"^num_ctx\s+(\d+)\s*$", parameters, flags=re.MULTILINE)
        return int(match.group(1)) if match else None


class LlamaServerBackend(LLMBackend):
    """Plain llama.cpp llama-server: /props reports the loaded n_ctx."""

    name = "llama-server"

    async def _props(self, client: httpx.AsyncClient, root: str) -> dict | None:
        response = await client.get(f"{root}/props")
        if not response.is_success:
            return None
        return _json_or_none(response)

    async def probe(self, client: httpx.AsyncClient, root: str) -> bool:
        payload = await self._props(client, root)
        return payload is not None and (
            "n_ctx" in payload or "default_generation_settings" in payload
        )

    async def get_n_ctx(
        self, client: httpx.AsyncClient, root: str, model: str
    ) -> int | None:
        payload = await self._props(client, root)
        if payload is None:
            return None
        value = payload.get("n_ctx")
        if not isinstance(value, int):
            nested = payload.get("default_generation_settings")
            value = nested.get("n_ctx") if isinstance(nested, dict) else None
        return value if isinstance(value, int) and value > 0 else None


_BACKENDS: tuple[LLMBackend, ...] = (
    LiteLLMBackend(),
    OllamaBackend(),
    LlamaServerBackend(),
)


async def discover_n_ctx(
    *,
    url: str,
    api_key: str | None,
    model: str,
    fallback_n_ctx: int,
    timeout_seconds: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, int]:
    """Detect the backend behind ``url`` and return (backend_name, n_ctx).

    Probes run in order (LiteLLM, Ollama, llama-server); the first match is
    asked for the model's context window. Probe/fetch failures of any kind
    degrade to the next backend or to ("generic", fallback_n_ctx) — discovery
    must never break the pipeline.
    """
    root = _base_root(url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(
        timeout=timeout_seconds, headers=headers, transport=transport
    ) as client:
        for backend in _BACKENDS:
            try:
                if not await backend.probe(client, root):
                    continue
                n_ctx = await backend.get_n_ctx(client, root, model)
            except httpx.HTTPError:
                continue
            if isinstance(n_ctx, int) and n_ctx > 0:
                return backend.name, n_ctx
            return backend.name, fallback_n_ctx
    return "generic", fallback_n_ctx
