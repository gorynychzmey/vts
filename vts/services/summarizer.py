from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from vts.services.diarization.merge import LABEL_WORDS

logger = logging.getLogger(__name__)

# How many tokenize round-trips may be in flight at once when the tokenizer
# is remote. Bounded so a long transcript cannot open thousands of sockets
# against llama.cpp at once; see `_tokenize_all`.
_TOKENIZE_CONCURRENCY = 16

# Utterances rendered by the diarization merge (render_cleaned_transcript in
# vts.services.diarization.merge) start with "<label> N: " only at the very
# start of the text, or right after a blank line ("\n\n") separating blocks —
# same-speaker text WITHIN a block is joined with a plain space and never
# contains a bare "\n". Matching at any line start (re.MULTILINE with "^")
# would also fire on a label-shaped string following a single embedded
# newline inside an ASR entry's own text (quoted speech, ASR artifacts),
# which truncates the real utterance and fabricates a wrongly-attributed one
# — the exact misattribution this whole feature exists to prevent. The
# lookbehind requires "start of string" or "\n\n" immediately before the
# label, matching the renderer's actual block boundary, not "any newline".
#
# The label word varies by recording language (label_map/speaker_label_word in
# merge.py — "Голос" for ru, "Speaker" otherwise), so this regex is built from
# merge.LABEL_WORDS rather than hardcoding "Голос". A splitter that only knew
# "Голос" would see an English-language diarized transcript's "Speaker 1: ..."
# as one undifferentiated blob and silently stop splitting on utterances.
_LABEL_ALTERNATION = "|".join(re.escape(word) for word in LABEL_WORDS)
# Registry person names replace "Голос N" in the rendered transcript (vts-552),
# so the splitter must also accept a bare "Вася: " / "Иван Петров: " label —
# otherwise a fully-named dialogue carries no recognisable label and collapses
# into one undifferentiated blob, silently disabling utterance-aware chunking.
#
# A name label is 1-3 whitespace-separated words, each starting with a letter
# (not a digit, so it cannot shadow the numbered form) and free of sentence
# punctuation. That word cap is what separates a label from prose that merely
# opens a block with a colon: "Именно поэтому мы решили так: ..." is 5 words and
# does not match. A <=3-word prose clause ("Итак: ...") is genuinely ambiguous
# with a one-word name and does match — accepted deliberately, since the cost is
# one utterance split in two, versus named dialogues never splitting at all.
_NAME_WORD = r"[^\W\d_][^\s:,;.!?]*"
_NAME_LABEL = rf"{_NAME_WORD}(?:[ ]{_NAME_WORD}){{0,2}}"
_UTTERANCE_RE = re.compile(
    rf"(?:^|(?<=\n\n))(?:(?:{_LABEL_ALTERNATION}) \d+|{_NAME_LABEL}): "
)


def split_utterances(text: str) -> list[str]:
    """Split rendered dialogue into whole utterances.

    Returns the whole text as one item when it carries no speaker labels, so
    undiarized transcripts flow through unchanged.
    """
    starts = [match.start() for match in _UTTERANCE_RE.finditer(text)]
    if not starts:
        return [text]
    # Text before the first label is a bare block: the renderer emits one for
    # audio diarization never covered, and refuses to attribute it to anyone.
    # Without this it would be dropped outright — a meeting opening on music or
    # crosstalk would silently never reach the summary.
    bounds = ([0] if starts[0] else []) + starts + [len(text)]
    return [text[bounds[i] : bounds[i + 1]].strip() for i in range(len(bounds) - 1)]


# Sentence splitting for chunking (deliberately NOT vts.metrics.quality.
# split_sentences). The metrics splitter breaks on every ".!?…" + whitespace,
# which is fine for redundancy statistics — a few extra fragments barely move a
# ratio — but wrong for chunking, where every fragment is a packing unit and a
# false boundary that lands on a window edge hands the model a truncated clause
# at the end of one window and its continuation at the start of the next.
#
# Measured false splits from the metrics splitter:
#   "Мы обсудили т.е. договорились."  -> ["Мы обсудили т.е.", "договорились."]
#   "Пришли А. Б. Иванов и т.д."      -> ["Пришли А.", "Б.", "Иванов и т.д."]
#   "Смотри стр. 15 в документе."     -> ["Смотри стр.", "15 в документе."]
#   "Это стоит 1. 5 млн"              -> ["Это стоит 1.", "5 млн"]
#
# The metrics splitter is left untouched: it is used for quality metrics, where
# its behaviour is established and changing it would silently shift recorded
# numbers.
#
# Common Russian/English abbreviations that end in a period without ending a
# sentence. Compared case-insensitively against the whitespace-delimited word
# immediately before the candidate boundary.
_ABBREVIATIONS = frozenset(
    {
        # Russian
        "т.е.", "т.д.", "т.п.", "т.к.", "т.н.", "и.о.", "др.", "пр.",
        "см.", "ср.", "стр.", "рис.", "табл.", "гл.", "п.", "пп.", "ст.",
        "рус.", "англ.", "г.", "гг.", "в.", "вв.", "руб.", "коп.", "тыс.",
        "млн.", "млрд.", "проф.", "доц.", "акад.", "им.", "ул.", "д.", "кв.",
        "обл.", "респ.", "тел.", "напр.", "прим.", "изд.", "ок.", "мин.",
        "сек.", "ч.", "мес.",
        # English
        "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "prof.",
        "st.", "fig.", "no.", "vol.", "pp.", "approx.", "inc.", "ltd.", "co.",
        "jr.", "sr.", "cf.", "al.",
    }
)

# A candidate boundary: sentence-ending punctuation followed by whitespace, or
# a blank line. The trailing whitespace is consumed so the pieces come back
# stripped, exactly as the metrics splitter returns them.
_CHUNK_SENT_SEP = re.compile(r"(?<=[.!?…])\s+|\n{2,}")

# A single capital letter plus a period is an initial ("А.", "Б.", "J."), not a
# sentence end. Written against the last whitespace-delimited word so
# "Иванов А." matches on "А." alone.
_INITIAL_RE = re.compile(r"^[^\W\d_]\.$", re.UNICODE)

# A bare number plus a period is a list marker or a split decimal ("1. 5 млн"),
# not a sentence end.
_NUMBER_DOT_RE = re.compile(r"^\d+\.$")


def _is_false_boundary(preceding: str) -> bool:
    """True when the text before a candidate boundary does not end a sentence.

    `preceding` is the text up to and including the boundary punctuation.
    Only "." can be an abbreviation or an initial; "!", "?" and "…" always
    end a sentence, so they are never suppressed.
    """
    if not preceding.endswith("."):
        return False
    word = preceding.rsplit(None, 1)[-1] if preceding.split() else ""
    if not word:
        return False
    lowered = word.lower()
    if lowered in _ABBREVIATIONS:
        return True
    # A single letter plus a period is an initial ("Иванов А. Б.") or the tail
    # of a spaced-out abbreviation: "и т.д." is one word only when written
    # without spaces, and a speaker who writes "и т. д." leaves a bare "д." as
    # the last word. Neither ends a sentence.
    if _INITIAL_RE.match(word):
        return True
    if _NUMBER_DOT_RE.match(word):
        return True
    return False


def split_sentences_for_chunking(text: str) -> list[str]:
    """Split text into packing units, keeping abbreviations intact.

    Unlike the metrics splitter this suppresses boundaries after known
    abbreviations, initials and numeric list markers, because here a false
    boundary can land on a window edge and truncate a clause.
    """
    if not text.strip():
        return []
    pieces: list[str] = []
    start = 0
    for match in _CHUNK_SENT_SEP.finditer(text):
        if _is_false_boundary(text[start : match.start()]):
            continue
        piece = text[start : match.start()].strip()
        if piece:
            pieces.append(piece)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


@lru_cache(maxsize=4)
def _load_tokenizer(path: str) -> "tokenizers.Tokenizer":  # type: ignore[name-defined]
    import tokenizers  # noqa: PLC0415

    return tokenizers.Tokenizer.from_file(path)


def _tokenize_local(path: str, text: str) -> list[int]:
    enc = _load_tokenizer(path).encode(text)
    return enc.ids


def _detokenize_local(path: str, token_ids: list[int]) -> str:
    return _load_tokenizer(path).decode(token_ids)


def _llama_server_base(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url[: -len("/v1")]
    return url


def _error_detail_from_body(body: bytes, *, max_len: int = 800) -> str:
    """Same extraction as `_response_error_detail`, from already-read bytes.

    A streamed response cannot be re-read with `.json()`, so an error body
    arrives as bytes from `aread()`. Reusing the extraction keeps failures
    readable ("Unsupported parameter: response_format") instead of degrading
    to a raw byte repr the moment the transport changed.
    """
    text = body.decode("utf-8", errors="replace")
    detail: str | None = None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        detail = _detail_from_payload(payload)
    if not detail:
        detail = text.strip() or "<empty response body>"
    compact = " ".join(detail.split())
    if len(compact) > max_len:
        return compact[: max_len - 3] + "..."
    return compact


def _detail_from_payload(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        if isinstance(error.get("type"), str):
            return str(error["type"]).strip()
    elif isinstance(error, str) and error.strip():
        return error.strip()
    elif isinstance(payload.get("message"), str):
        return str(payload["message"]).strip()
    return None


def _response_error_detail(response: httpx.Response, *, max_len: int = 800) -> str:
    detail: str | None = None
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                detail = message.strip()
            elif isinstance(error.get("type"), str):
                detail = str(error["type"]).strip()
        elif isinstance(error, str) and error.strip():
            detail = error.strip()
        elif isinstance(payload.get("message"), str):
            detail = str(payload["message"]).strip()

    if not detail:
        text = response.text.strip()
        if text:
            detail = text
    if not detail:
        detail = "<empty response body>"
    compact = " ".join(detail.split())
    if len(compact) > max_len:
        return compact[: max_len - 3] + "..."
    return compact


def _raise_with_response_details(response: httpx.Response, *, context: str) -> None:
    request_url = "<unknown>"
    if response.request is not None:
        request_url = str(response.request.url)
    detail = _response_error_detail(response)
    raise RuntimeError(
        f"{context} failed with HTTP {response.status_code} for {request_url}: {detail}"
    )


def _is_loading_model_body(status_code: int, body: bytes) -> bool:
    """Same "model is loading" test as `_is_loading_model_response`, on bytes.

    A streamed response cannot be re-read with `.json()`, so the error body
    arrives from `aread()`. Sharing the predicate keeps the streaming path
    from silently disagreeing with the tokenize/detokenize path about what a
    cold model looks like.
    """
    if status_code not in (503, 529):
        return False
    detail = _error_detail_from_body(body).lower()
    return "loading model" in detail or "model is loading" in detail


def _is_loading_model_response(response: httpx.Response) -> bool:
    if response.status_code not in (503, 529):
        return False
    detail = _response_error_detail(response).lower()
    return "loading model" in detail or "model is loading" in detail


async def _post_with_loading_retry(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    payload: dict[str, Any],
    loading_wait_seconds: float,
) -> httpx.Response:
    response = await client.post(endpoint, json=payload)
    elapsed = 0.0
    attempt = 0
    while _is_loading_model_response(response) and elapsed < loading_wait_seconds:
        delay = min(0.5 * (2**attempt), 5.0)
        wait_for = min(delay, loading_wait_seconds - elapsed)
        if wait_for <= 0:
            break
        await asyncio.sleep(wait_for)
        elapsed += wait_for
        response = await client.post(endpoint, json=payload)
        attempt += 1
    return response


def _loading_wait_seconds(timeout_seconds: int, *, cap_seconds: float = 120.0) -> float:
    return max(5.0, min(float(timeout_seconds) * 0.6, cap_seconds))


def derive_stream_ceiling(
    max_tokens: int | None,
    *,
    min_tokens_per_second: float,
    slack: float,
    floor_seconds: int,
    cap_seconds: int,
) -> float:
    """Overall wall-clock ceiling for one streamed completion.

    Scaled to the work requested rather than fixed: a segment window and a
    final summary differ by an order of magnitude in output size, and one
    constant cannot serve both without being far too loose for the small case.

    A missing budget means "we do not know how much this will produce", so the
    cap is the only defensible answer — never the floor, which would cut off
    legitimate long work.
    """
    if max_tokens is None or max_tokens <= 0:
        return float(cap_seconds)
    raw = max_tokens / max(min_tokens_per_second, 0.1) * slack
    return float(max(floor_seconds, min(raw, cap_seconds)))


def parse_sse_content(line: str) -> str | None:
    """Text carried by one SSE line, or None if it carries none.

    Returns None rather than raising on malformed JSON: a stream can legally
    interleave comments and keep-alives, and one unparsable frame should not
    fail a completion that is otherwise arriving normally.

    Empty-string content counts as no content, so it cannot reset the idle
    timer — otherwise a backend emitting empty deltas would look alive while
    producing nothing.
    """
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        content = parsed["choices"][0]["delta"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str) or not content:
        return None
    return content


def is_stream_finished(line: str) -> bool:
    """True when this SSE line says the completion ended on its own terms.

    Two markers count, because LiteLLM sits between us and the model and is
    not obliged to reproduce OpenAI's framing byte for byte: the `[DONE]`
    sentinel, and any chunk carrying a non-empty `finish_reason`. Accepting
    either keeps a legitimate response from failing just because the proxy
    dropped one of the two.

    Everything else — content deltas, keep-alives, malformed frames — is not
    an ending, so a body that simply stops mid-generation is distinguishable
    from one that finished.
    """
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return False
    payload = stripped[len("data:") :].strip()
    if payload == "[DONE]":
        return True
    if not payload:
        return False
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return False
    try:
        choices = parsed["choices"]
    except (KeyError, TypeError):
        return False
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason.strip():
            return True
    return False


class StreamInterrupted(RuntimeError):
    """A streamed completion started producing text and then broke.

    Distinct from `StreamTimeout` (our own limits fired) and from a plain
    transport error before the first chunk (the payload queue's business).
    Once content has arrived the backend has accepted the request shape, so
    there is nothing left for the fallback queue to discover: replaying the
    identical prompt through seven more variants only burns GPU minutes on a
    result nobody is waiting for. Raising a dedicated type lets the caller
    tell "never started" from "broke halfway" without inspecting httpx
    internals.
    """

    def __init__(
        self,
        reason: str,
        *,
        chunks: int,
        elapsed: float,
        cause: BaseException | None = None,
    ) -> None:
        detail = f": {cause.__class__.__name__}" if cause is not None else ""
        super().__init__(
            f"llm stream interrupted ({reason}) after {elapsed:.1f}s "
            f"and {chunks} chunks{detail}"
        )
        self.reason = reason
        self.chunks = chunks
        self.elapsed = elapsed
        self.cause = cause


class StreamTimeout(RuntimeError):
    """A streamed completion hit one of its three limits.

    Carries what was seen so the caller can log it: the whole point of this
    feature is that a stalled request reports how far it got instead of going
    silent for 17 minutes.
    """

    def __init__(self, reason: str, *, chunks: int, elapsed: float) -> None:
        super().__init__(
            f"llm stream {reason} timeout after {elapsed:.1f}s and {chunks} chunks"
        )
        self.reason = reason
        self.chunks = chunks
        self.elapsed = elapsed


async def read_sse_stream(
    lines: AsyncIterator[str],
    *,
    first_chunk_timeout: float,
    idle_timeout: float,
    ceiling: float,
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[int, float], None] | None = None,
) -> str:
    """Accumulate an SSE completion, enforcing three independent limits.

    The limits answer different questions. `first_chunk_timeout` covers model
    load, which is slow and says nothing about health. `idle_timeout` is the
    real stall detector once text is flowing. `ceiling` is a backstop against
    generation that never ends.

    `clock` is injectable so tests drive time without sleeping.
    """
    started = clock()
    last_seen = started
    chunks = 0
    finished = False
    buffer: list[str] = []

    iterator = lines.__aiter__()
    while True:
        try:
            line = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except (StreamTimeout, StreamInterrupted):
            raise
        except asyncio.CancelledError:
            # Cancellation is the feature working as designed (the task was
            # cancelled and the socket closes, stopping the GPU) — it must
            # propagate untouched, not be reclassified as a broken stream.
            raise
        except Exception as exc:
            # Transport died while reading the body. Before the first chunk
            # this is the payload queue's business and is re-raised as-is;
            # after it, the request shape was already accepted, so retrying
            # or walking the queue would only replay a full generation.
            if not chunks:
                raise
            raise StreamInterrupted(
                "transport",
                chunks=chunks,
                elapsed=clock() - started,
                cause=exc,
            ) from exc

        now = clock()
        if now - started > ceiling:
            raise StreamTimeout("ceiling", chunks=chunks, elapsed=now - started)
        limit = idle_timeout if chunks else first_chunk_timeout
        if now - last_seen > limit:
            reason = "idle" if chunks else "first_chunk"
            raise StreamTimeout(reason, chunks=chunks, elapsed=now - started)
        if is_stream_finished(line):
            finished = True
        content = parse_sse_content(line)
        if content is None:
            continue
        buffer.append(content)
        chunks += 1
        last_seen = now
        if on_progress is not None and chunks % 100 == 0:
            on_progress(chunks, now - started)

    now = clock()
    if not chunks:
        # The stream ended without a single content chunk — whether that took
        # 0s (empty body, upstream cut the connection immediately) or the full
        # timeout, it is never a valid empty completion. Silently returning ""
        # here would let a stalled or broken upstream look like success.
        raise StreamTimeout("first_chunk", chunks=0, elapsed=now - started)
    if not finished:
        # Content arrived, then the body ended with neither `[DONE]` nor a
        # `finish_reason`. That is a truncated response — a proxy cut the
        # connection, upstream closed early, a hop lost the tail — and the
        # half-written summary it produced is indistinguishable from a
        # complete one downstream. Half a summary stored as a success is
        # worse than a loud failure.
        raise StreamInterrupted("truncated", chunks=chunks, elapsed=now - started)
    return "".join(buffer)


def _build_chat_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int | None,
    include_response_format: bool,
    temperature: float,
    top_p: float | None = None,
    min_p: float | None = None,
    repeat_penalty: float | None = None,
    max_tokens_key: str = "max_tokens",
    include_model: bool = True,
    model_override: str | None = None,
    thinking: bool | None = None,
    num_ctx: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        # Every chat completion is streamed. This is not a performance
        # tweak: with a blocking POST, a client that walks away leaves the
        # backend generating into a socket nobody reads (measured: 17m38s of
        # GPU after a 20s client timeout). Streaming makes the disconnect
        # propagate, so closing the response stops generation upstream.
        "stream": True,
    }
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if top_p is not None:
        payload["top_p"] = top_p
    if min_p is not None:
        payload["min_p"] = min_p
    if repeat_penalty is not None:
        payload["repeat_penalty"] = repeat_penalty
    if include_response_format:
        payload["response_format"] = {"type": "json_object"}
    selected_model = model_override if model_override is not None else model
    if include_model and selected_model.strip():
        payload["model"] = selected_model
    if max_tokens is not None:
        payload[max_tokens_key] = max_tokens
    if num_ctx is not None:
        payload["num_ctx"] = num_ctx
    return payload


def _model_name_variants(model: str) -> list[str]:
    value = model.strip()
    if not value:
        return []
    candidates: list[str] = []

    def add(item: str) -> None:
        normalized = item.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(value)
    if value.endswith(".gguf"):
        add(value[: -len(".gguf")])
    else:
        add(f"{value}.gguf")
    if "/" in value:
        basename = value.rsplit("/", 1)[-1]
        add(basename)
        if basename.endswith(".gguf"):
            add(basename[: -len(".gguf")])
        else:
            add(f"{basename}.gguf")
    return candidates


class LLMClient:
    def __init__(self, *, url: str, api_key: str | None = None) -> None:
        self.url = url
        self._headers: dict[str, str] = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )

    def _client(self, timeout_seconds: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_seconds, headers=self._headers)

    def _stream_client(
        self,
        timeout_seconds: int,
        *,
        first_chunk_timeout: float,
        idle_timeout: float,
    ) -> httpx.AsyncClient:
        """Client for streamed completions, with an explicit socket read timeout.

        The reader's three limits (first chunk, idle, ceiling) are all checked
        when a line arrives, so they can only fire while the stream is
        producing. If the upstream accepts the connection and then says
        nothing at all, `async for` never yields and no limit is ever
        evaluated — the read would hang for as long as the transport allows.
        The socket-level read timeout is the only thing that can break that
        deadlock, so it must always be set.

        A socket read here spans exactly one gap between bytes, so the read
        timeout only has to outlast the longest gap our own limits tolerate:
        model load before the first chunk, and a pause between chunks after
        it. Hence `max(first_chunk_timeout, idle_timeout)` — dropping below
        either would let the transport fire first, turning a clean
        StreamTimeout (which reports how many chunks arrived) into a bare
        ReadTimeout that also walks the whole fallback queue. Both limits are
        operator-tunable, so both must be in the floor.

        `timeout_seconds` is deliberately NOT inherited. It is a whole-request
        budget, and using it per read multiplies it by the number of payload
        variants on a hung backend (a 1200s warmup across 8 variants would
        allow 160 minutes). The ceiling is what bounds total generation time.
        Connect/write/pool stay on the caller's budget.
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(timeout_seconds),
                read=max(first_chunk_timeout, idle_timeout),
            ),
            headers=self._headers,
        )

    async def _list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        endpoint = self.url.rstrip("/") + "/models"
        try:
            response = await client.get(endpoint)
        except httpx.HTTPError:
            return []
        if not response.is_success:
            return []
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        result: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id.strip():
                result.append(model_id.strip())
        return result

    async def tokenize(
        self,
        *,
        model: str,
        text: str,
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> list[int]:
        if tokenizer_path:
            return _tokenize_local(tokenizer_path, text)
        endpoint = _llama_server_base(self.url) + "/tokenize"
        payload: dict[str, Any] = {"content": text}
        if model:
            payload["model"] = model
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds, cap_seconds=90.0)
        async with self._client(timeout_seconds) as client:
            response = await _post_with_loading_retry(
                client=client,
                endpoint=endpoint,
                payload=payload,
                loading_wait_seconds=loading_wait_seconds,
            )
            if response.status_code == 400 and model:
                retry_response = await _post_with_loading_retry(
                    client=client,
                    endpoint=endpoint,
                    payload={"content": text},
                    loading_wait_seconds=loading_wait_seconds,
                )
                if retry_response.is_success:
                    response = retry_response
                else:
                    recovered = False
                    for variant in _model_name_variants(model)[1:]:
                        model_retry = await _post_with_loading_retry(
                            client=client,
                            endpoint=endpoint,
                            payload={"content": text, "model": variant},
                            loading_wait_seconds=loading_wait_seconds,
                        )
                        if model_retry.is_success:
                            response = model_retry
                            recovered = True
                            break
                    if not recovered:
                        available_models = await self._list_models(client=client)
                        if available_models and model not in available_models:
                            server_model = available_models[0]
                            model_retry = await _post_with_loading_retry(
                                client=client,
                                endpoint=endpoint,
                                payload={"content": text, "model": server_model},
                                loading_wait_seconds=loading_wait_seconds,
                            )
                            if model_retry.is_success:
                                response = model_retry
                                recovered = True
                            else:
                                _raise_with_response_details(
                                    model_retry,
                                    context=f"llama tokenize (retry with model={server_model})",
                                )
                    if not recovered:
                        _raise_with_response_details(
                            retry_response,
                            context="llama tokenize (retry without model)",
                        )
        if not response.is_success:
            _raise_with_response_details(response, context="llama tokenize")
        data = response.json()
        tokens = data.get("tokens")
        if not isinstance(tokens, list):
            raise RuntimeError("Invalid llama.cpp tokenize response format")
        return [int(token) for token in tokens]

    async def detokenize(
        self,
        *,
        model: str,
        tokens: list[int],
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> str:
        if tokenizer_path:
            return _detokenize_local(tokenizer_path, tokens)
        endpoint = _llama_server_base(self.url) + "/detokenize"
        payload: dict[str, Any] = {"tokens": tokens}
        if model:
            payload["model"] = model
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds, cap_seconds=90.0)
        async with self._client(timeout_seconds) as client:
            response = await _post_with_loading_retry(
                client=client,
                endpoint=endpoint,
                payload=payload,
                loading_wait_seconds=loading_wait_seconds,
            )
            if response.status_code == 400 and model:
                retry_response = await _post_with_loading_retry(
                    client=client,
                    endpoint=endpoint,
                    payload={"tokens": tokens},
                    loading_wait_seconds=loading_wait_seconds,
                )
                if retry_response.is_success:
                    response = retry_response
                else:
                    recovered = False
                    for variant in _model_name_variants(model)[1:]:
                        model_retry = await _post_with_loading_retry(
                            client=client,
                            endpoint=endpoint,
                            payload={"tokens": tokens, "model": variant},
                            loading_wait_seconds=loading_wait_seconds,
                        )
                        if model_retry.is_success:
                            response = model_retry
                            recovered = True
                            break
                    if not recovered:
                        available_models = await self._list_models(client=client)
                        if available_models and model not in available_models:
                            server_model = available_models[0]
                            model_retry = await _post_with_loading_retry(
                                client=client,
                                endpoint=endpoint,
                                payload={"tokens": tokens, "model": server_model},
                                loading_wait_seconds=loading_wait_seconds,
                            )
                            if model_retry.is_success:
                                response = model_retry
                                recovered = True
                            else:
                                _raise_with_response_details(
                                    model_retry,
                                    context=f"llama detokenize (retry with model={server_model})",
                                )
                    if not recovered:
                        _raise_with_response_details(
                            retry_response,
                            context="llama detokenize (retry without model)",
                        )
        if not response.is_success:
            _raise_with_response_details(response, context="llama detokenize")
        data = response.json()
        content = data.get("content")
        if not isinstance(content, str):
            raise RuntimeError("Invalid llama.cpp detokenize response format")
        return content

    async def chunk_text(
        self,
        *,
        text: str,
        model: str,
        window_tokens: int = 2000,
        tokenizer_path: str | None = None,
        split_on_utterances: bool = False,
    ) -> list[str]:
        # There is no `overlap_ratio`: chunking packs whole units and never
        # overlaps them. An overlap existed to stitch context torn mid-sentence
        # by a sliding token window; packing whole sentences tears nothing, so
        # an overlap would only duplicate text. On the segment stage that
        # duplication is visible in the output — the stage cleans sentence by
        # sentence rather than summarizing, so a passage processed twice
        # appears twice. Measured on production 2026-08-19: 585 characters,
        # 14.7% of one window, repeated verbatim from the previous one.
        if not text.strip():
            return []
        if split_on_utterances:
            return await self._chunk_by_utterances(
                text=text,
                model=model,
                window_tokens=window_tokens,
                tokenizer_path=tokenizer_path,
            )
        return await self._pack_units(
            units=split_sentences_for_chunking(text),
            model=model,
            window_tokens=window_tokens,
            tokenizer_path=tokenizer_path,
            joiner=" ",
            labeled=False,
        )

    async def _pack_units(
        self,
        *,
        units: list[str],
        model: str,
        window_tokens: int,
        tokenizer_path: str | None,
        joiner: str,
        labeled: bool,
    ) -> list[str]:
        """Pack whole units into windows without overlap.

        Shared by both chunking modes because the argument is the same at
        either granularity: a window that ends on a unit boundary tears
        nothing, so there is no torn context for an overlap to stitch — and an
        overlap would instead duplicate the text it repeats. `units` are
        utterances for a diarized transcript and sentences otherwise; the only
        difference is what counts as indivisible, and how the pieces are
        joined back together.

        `labeled` says whether a unit may carry a speaker label. Only the
        diarized path sets it. A sentence never carries one, and the label
        regex deliberately matches short prose openings ("Итак: ", "Вывод: ")
        — see the `_NAME_LABEL` comment — so letting the sentence path detect
        labels for itself would prepend a fabricated speaker name to every
        continuation of a long sentence.

        A unit longer than the whole window is cut by tokens as a last resort
        (`_split_long_utterance`), which is the one place a tear is
        unavoidable.
        """
        sizes = await self._tokenize_all(
            texts=units, model=model, tokenizer_path=tokenizer_path
        )

        chunks: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for unit, tokens in zip(units, sizes, strict=True):
            size = len(tokens)

            if size > window_tokens:
                if current:
                    chunks.append(joiner.join(current))
                    current, current_tokens = [], 0
                label = ""
                if labeled:
                    match = _UTTERANCE_RE.match(unit)
                    label = match.group(0) if match else ""
                chunks.extend(
                    await self._split_long_utterance(
                        utterance=unit,
                        tokens=tokens,
                        model=model,
                        window_tokens=window_tokens,
                        tokenizer_path=tokenizer_path,
                        label=label,
                    )
                )
                continue

            if current_tokens + size > window_tokens and current:
                chunks.append(joiner.join(current))
                current, current_tokens = [], 0
            current.append(unit)
            current_tokens += size

        if current:
            chunks.append(joiner.join(current))
        return chunks

    async def _chunk_by_utterances(
        self,
        *,
        text: str,
        model: str,
        window_tokens: int,
        tokenizer_path: str | None,
    ) -> list[str]:
        """Pack whole utterances into windows, keeping speaker labels intact."""
        return await self._pack_units(
            units=split_utterances(text),
            model=model,
            window_tokens=window_tokens,
            tokenizer_path=tokenizer_path,
            joiner="\n\n",
            labeled=True,
        )

    async def _tokenize_all(
        self,
        *,
        texts: list[str],
        model: str,
        tokenizer_path: str | None,
    ) -> list[list[int]]:
        """Tokenize every unit, in bounded-concurrency waves when remote.

        Packing needs a token count per unit, and counts are not additive
        across a boundary, so there is no way to derive them from one call over
        the joined text. With `tokenizer_path` set the tokenizer is a local BPE
        and the calls are cheap function calls, so they run in order.

        Without it, `tokenize` is an HTTP round-trip to llama.cpp — and
        `llm_tokenizer_path` defaults to None, so this is the shipped
        configuration, not an edge case. One call per sentence is thousands of
        SEQUENTIAL round-trips (measured: 3000 for a 388k-character
        transcript), which is minutes of pure latency. Issuing them in waves
        turns that into (n / _TOKENIZE_CONCURRENCY) round-trip times while
        keeping each unit's count exact. The wave is bounded rather than
        unbounded so a long transcript cannot open thousands of sockets at
        once and be throttled or refused by the server.
        """
        if tokenizer_path:
            return [
                await self.tokenize(model=model, text=text, tokenizer_path=tokenizer_path)
                for text in texts
            ]
        results: list[list[int]] = []
        for start in range(0, len(texts), _TOKENIZE_CONCURRENCY):
            wave = texts[start : start + _TOKENIZE_CONCURRENCY]
            results.extend(
                await asyncio.gather(
                    *(
                        self.tokenize(model=model, text=text, tokenizer_path=tokenizer_path)
                        for text in wave
                    )
                )
            )
        return results

    async def _split_long_utterance(
        self,
        *,
        utterance: str,
        tokens: list[int],
        model: str,
        window_tokens: int,
        tokenizer_path: str | None,
        label: str,
    ) -> list[str]:
        """Cut an over-long unit by tokens, repeating the caller's label.

        A ten-minute monologue inside a meeting cannot fit a window, so the
        budget wins — but every continuation carries the label, or the tail
        would be attributed to whoever spoke before.

        The label is supplied by the caller, never detected here: this
        function serves both an utterance (which has a speaker label) and a
        sentence (which does not). `_UTTERANCE_RE` matches short prose
        openings such as "Итак: " or "Вывод: " by design, so detecting the
        label locally would turn the first words of a long sentence into a
        fabricated speaker name repeated across every continuation — the
        undiarized path passes "".

        The label is prepended as extra text after detokenizing the body, so
        its tokens must be charged against window_tokens BEFORE slicing —
        otherwise every continuation chunk comes back over budget by exactly
        the label's token count (label_tokens full-window body + label on
        top). We tokenize the label once and slice each body at
        `window_tokens - label_tokens`.

        Degenerate case: if the label alone meets or exceeds window_tokens,
        there is no budget left for any body text. Truncating the label would
        make attribution illegible (worse than a temporarily over-budget
        chunk), so we never do that — instead we floor the body slice at 1
        token per chunk. This keeps the label intact and guarantees cursor
        always advances, so the loop terminates; the resulting chunk is
        allowed to exceed window_tokens in this narrow, pathological case
        (label >= window) since there is no way to satisfy the budget and
        keep the label whole at the same time.
        """
        label_tokens = (
            len(await self.tokenize(model=model, text=label, tokenizer_path=tokenizer_path))
            if label
            else 0
        )
        # Reserve room for the label in every continuation chunk. Floor at 1
        # so a label at/over the window still makes progress each iteration.
        body_budget = max(window_tokens - label_tokens, 1)

        parts: list[str] = []
        cursor = 0
        while cursor < len(tokens):
            part = tokens[cursor : cursor + body_budget]
            body = await self.detokenize(model=model, tokens=part, tokenizer_path=tokenizer_path)
            body = body.strip()
            if not body:
                break
            parts.append(body if body.startswith(label.strip()) and label else f"{label}{body}")
            cursor += body_budget
        return parts

    async def chat_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: int = 600,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        top_p: float | None = None,
        min_p: float | None = None,
        repeat_penalty: float | None = None,
        # Accepted for call compatibility but no longer consulted: it governed
        # the blocking POST's retry loop, and a streamed request must not be
        # replayed. Recovery before the first chunk comes from the payload
        # queue; after it, nothing is retried at all (vts-94wf).
        request_attempts: int = 3,
        use_json_format: bool = True,
        thinking: bool | None = None,
        num_ctx: int | None = None,
        stream_idle_timeout: float = 120.0,
        stream_first_chunk_timeout: float = 300.0,
        min_tokens_per_second: float = 3.0,
        ceiling_slack: float = 1.5,
        ceiling_floor_seconds: int = 300,
        ceiling_cap_seconds: int = 3600,
    ) -> str:
        endpoint = self.url.rstrip("/") + "/chat/completions"
        queue: list[tuple[str, dict[str, Any]]] = []
        seen_payloads: set[str] = set()

        def enqueue(label: str, payload: dict[str, Any]) -> None:
            key = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if key in seen_payloads:
                return
            seen_payloads.add(key)
            queue.append((label, payload))

        common = dict(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            min_p=min_p,
            repeat_penalty=repeat_penalty,
            num_ctx=num_ctx,
        )

        enqueue("default", _build_chat_payload(**common, include_response_format=use_json_format, thinking=thinking))
        model_variants = _model_name_variants(model)
        for variant in model_variants[1:]:
            enqueue(f"default_model_variant:{variant}", _build_chat_payload(**common, model_override=variant, include_response_format=use_json_format))
            enqueue(f"without_response_format_model_variant:{variant}", _build_chat_payload(**common, model_override=variant, include_response_format=False))
            if max_tokens is not None:
                enqueue(f"without_response_format_model_variant:{variant}:max_completion_tokens", _build_chat_payload(**common, model_override=variant, include_response_format=False, max_tokens_key="max_completion_tokens"))
        enqueue("without_response_format", _build_chat_payload(**common, include_response_format=False, thinking=thinking))
        if max_tokens is not None:
            enqueue("without_response_format_max_completion_tokens", _build_chat_payload(**common, include_response_format=False, max_tokens_key="max_completion_tokens", thinking=thinking))
        enqueue("without_response_format_without_model", _build_chat_payload(**common, include_response_format=False, thinking=thinking, include_model=False))
        if max_tokens is not None:
            enqueue("without_response_format_without_model_max_completion_tokens", _build_chat_payload(**common, include_response_format=False, max_tokens_key="max_completion_tokens", thinking=thinking, include_model=False))

        failures: list[str] = []
        discovered_model_fallback = False
        loading_wait_seconds = _loading_wait_seconds(timeout_seconds)
        loading_waited = 0.0
        loading_attempts = 0
        async with self._stream_client(
            timeout_seconds,
            first_chunk_timeout=stream_first_chunk_timeout,
            idle_timeout=stream_idle_timeout,
        ) as client:
            while queue:
                label, payload = queue.pop(0)
                ceiling = derive_stream_ceiling(
                    max_tokens,
                    min_tokens_per_second=min_tokens_per_second,
                    slack=ceiling_slack,
                    floor_seconds=ceiling_floor_seconds,
                    cap_seconds=ceiling_cap_seconds,
                )

                def _log_progress(chunks: int, elapsed: float) -> None:
                    rate = chunks / elapsed if elapsed > 0 else 0.0
                    logger.info(
                        "llm stream progress: %s chunks in %.0fs (%.1f chunks/s)",
                        chunks,
                        elapsed,
                        rate,
                    )

                try:
                    async with client.stream("POST", endpoint, json=payload) as response:
                        if not response.is_success:
                            body = await response.aread()
                            if _is_loading_model_body(response.status_code, body):
                                # A cold model answers 503 "loading model"
                                # *before* the stream opens, so no streaming
                                # limit can cover it — `stream_first_chunk_
                                # timeout` only starts once there is a body to
                                # read. Waiting here is what the blocking path
                                # did via `_post_with_loading_retry`, and
                                # PrepareLlamaModelStep exists precisely to
                                # sit through this load (~75s in production).
                                if loading_waited < loading_wait_seconds:
                                    delay = min(
                                        0.5 * (2**loading_attempts),
                                        5.0,
                                        loading_wait_seconds - loading_waited,
                                    )
                                    if delay > 0:
                                        logger.info(
                                            "llm backend is loading the model; "
                                            "waited %.0fs of %.0fs",
                                            loading_waited,
                                            loading_wait_seconds,
                                        )
                                        await asyncio.sleep(delay)
                                        loading_waited += delay
                                        loading_attempts += 1
                                        # Same payload, same position: the
                                        # shape was never rejected, the model
                                        # simply was not up yet.
                                        queue.insert(0, (label, payload))
                                        continue
                            failures.append(
                                f"{label}: HTTP {response.status_code} "
                                f"({_error_detail_from_body(body)})"
                            )
                            if response.status_code != 400:
                                raise RuntimeError(
                                    f"llama chat completion failed for {endpoint}: {failures[-1]}"
                                )
                            if not discovered_model_fallback and model.strip():
                                discovered_model_fallback = True
                                available_models = await self._list_models(client=client)
                                if available_models and model not in available_models:
                                    server_model = available_models[0]
                                    enqueue(
                                        f"server_model:{server_model}",
                                        _build_chat_payload(
                                            **common,
                                            model_override=server_model,
                                            include_response_format=False,
                                        ),
                                    )
                                    if max_tokens is not None:
                                        enqueue(
                                            f"server_model:{server_model}:max_completion_tokens",
                                            _build_chat_payload(
                                                **common,
                                                model_override=server_model,
                                                include_response_format=False,
                                                max_tokens_key="max_completion_tokens",
                                            ),
                                        )
                            continue
                        text = await read_sse_stream(
                            response.aiter_lines(),
                            first_chunk_timeout=stream_first_chunk_timeout,
                            idle_timeout=stream_idle_timeout,
                            ceiling=ceiling,
                            on_progress=_log_progress,
                        )
                except StreamInterrupted as exc:
                    # The backend already accepted this payload and started
                    # generating, so the fallback queue has nothing left to
                    # discover and a retry would replay the whole generation.
                    # The spec is explicit: "stream breaks mid-generation →
                    # fail, no retry".
                    logger.warning(
                        "llm stream interrupted (%s): %s chunks in %.0fs",
                        exc.reason,
                        exc.chunks,
                        exc.elapsed,
                    )
                    raise
                except StreamTimeout as exc:
                    # Deliberately no retry and no fallback: the prompt is
                    # byte-identical, so a second attempt cannot do better —
                    # it only burns the GPU (vts-94wf).
                    logger.warning(
                        "llm stream %s timeout: %s chunks in %.0fs",
                        exc.reason,
                        exc.chunks,
                        exc.elapsed,
                    )
                    raise
                except httpx.TransportError as exc:
                    # The payload queue answers one question — which request
                    # shape does this backend accept — and a backend that
                    # dislikes the shape says so promptly with HTTP 400. A
                    # transport error answers a different question: the
                    # request either never arrived (ConnectError, which is
                    # indistinguishable from a firewall or a downed network)
                    # or arrived and went unanswered (ReadTimeout, meaning
                    # busy or wedged). Rewording the payload addresses
                    # neither, and each variant costs a full generation:
                    # measured on production, a 73k-token window walked all
                    # eight variants, holding the GPU for the whole parade.
                    #
                    # TransportError, not HTTPError: the latter also covers
                    # HTTPStatusError, which is exactly the HTTP 400 the queue
                    # exists to walk. Nothing calls raise_for_status() on this
                    # path today, so the wider clause is harmless right now —
                    # and would silently kill the queue the first time someone
                    # adds one.
                    #
                    # Wrapped rather than re-raised bare because the transport
                    # exceptions carry no message: str(httpx.ReadTimeout(""))
                    # is the EMPTY STRING, and process_task puts str(exc)
                    # straight into error_message, the SSE event and the push.
                    # A user would see a blank error and classify_failure_code
                    # would get "" and return None. ReadTimeout is the very
                    # case seen on production.
                    detail = str(exc) or "no detail"
                    raise RuntimeError(
                        f"llama chat completion transport error for {endpoint}: "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc
                return text
            else:
                attempts = "; ".join(failures) if failures else "no attempts executed"
                raise RuntimeError(
                    f"llama chat completion failed after retries for {endpoint}: {attempts}"
                )
        raise RuntimeError(
            f"llama chat completion produced no result for {endpoint}"
        )

    async def count_tokens(
        self,
        *,
        text: str,
        model: str,
        timeout_seconds: int = 120,
        tokenizer_path: str | None = None,
    ) -> int:
        tokens = await self.tokenize(
            model=model,
            text=text,
            timeout_seconds=timeout_seconds,
            tokenizer_path=tokenizer_path,
        )
        return len(tokens)


def load_prompt(prompts_dir: Path, filename: str, fallback: str) -> str:
    path = prompts_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def tokens_to_words(tokens: int) -> int:
    """Approximate word count from a token count (≈0.75 words per token)."""
    return max(1, round(tokens * 0.75))


def inject_budget_vars(
    prompt: str,
    *,
    input_tokens: int | None = None,
    target_tokens: int | None = None,
    target_ratio: float | None = None,
) -> str:
    """Replace budget placeholders in *prompt*.

    Supported placeholders: ``${INPUT_WORDS}``, ``${TARGET_WORDS}``,
    ``${TARGET_RATIO}``.
    """
    if input_tokens is not None:
        prompt = prompt.replace("${INPUT_WORDS}", str(tokens_to_words(input_tokens)))
    if target_tokens is not None:
        prompt = prompt.replace("${TARGET_WORDS}", str(tokens_to_words(target_tokens)))
    if target_ratio is not None:
        prompt = prompt.replace("${TARGET_RATIO}", str(round(target_ratio * 100)))
    return prompt


def parse_json_response(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "summary": raw}
    if isinstance(payload, dict):
        return payload
    return {"raw": raw, "summary": str(payload)}
