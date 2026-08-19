# vts-gouq — LLM streaming: interruptible generation and visible progress

**Beads:** vts-gouq (related: vts-94wf, vts-4jsi)
**Date:** 2026-08-19
**Status:** Design approved, ready for implementation plan

## Scope

Move `chat_completion` from a blocking POST to an SSE stream, so that closing
the connection stops generation on the backend, progress is visible while a
window is being written, and timeouts key off the gap between tokens instead of
total elapsed time.

**In scope:**
- `SummarizerClient.chat_completion` reads a stream and accumulates the text.
- Idle timeout between chunks; a separate, longer budget for the first chunk.
- An overall ceiling derived from `max_tokens` (not a fixed constant).
- Periodic progress logging: tokens received, elapsed, tokens/s.
- No retries on either timeout (only genuine network errors are retried).

**Out of scope, deliberately:**
- Streaming the text onward to the browser. The function still returns a
  complete `str`; call sites are untouched. Live text in the UI is a separate
  feature with its own event-layer cost.
- A user-facing Cancel action. Forced cancellation already exists
  (`WorkerPool.watch_cancels` → `atask.cancel()`); exposing it in the UI is a
  separate task.
- Fixing `max_tokens` / `cache_prompt` being dropped on the way to Ollama
  (see *Known backend gaps*).

## Background — why generation could not be stopped

Measured on the production host, 2026-08-19.

`chat_completion` issues one blocking `client.post(...)`. When the client goes
away — a timeout, a cancelled task — the request is abandoned, but **the model
keeps generating**. A direct measurement: the client disconnected 20 seconds
into a request; Ollama logged `200 | 17m38s` and held the GPU for the whole of
it, then answered into a closed socket.

Two layers had to be fixed for cancellation to work at all:

1. **No streaming.** A blocking POST gives the backend nothing to notice: it
   only writes at the end. With `stream: true`, Ollama detects the broken pipe
   on its first failed chunk write.
2. **LiteLLM did not propagate the disconnect.** Under litellm 1.83.0 the proxy
   stopped reading the upstream stream but never closed it — upstream
   [#30244](https://github.com/BerriAI/litellm/issues/30244), fixed by
   [#30245](https://github.com/BerriAI/litellm/pull/30245). `1.83.0` did not
   contain the fix; the proxy has since been upgraded to **1.97.0**, which
   does (`_UpstreamClosingStreamingResponse`).

After the upgrade, the same disconnect test closes the Ollama request **in the
same second**. Streaming is therefore a precondition for cancellation, and the
proxy version is a precondition for streaming to help.

## Why this is worth doing

Three separate production problems share this root.

**Generation could not be stopped.** Cancelling a task freed the worker but not
the GPU — up to ~17 minutes of compute spent on a result nobody would read.

**A running window was indistinguishable from a hang.** Between
`gpu slot acquired` and the result there is no output for 10–17 minutes. On
2026-08-18 a task appeared frozen for 22 minutes; only the Ollama log revealed
it was a retry loop, not a hang.

**Timeouts measured the wrong thing.** A total-duration timeout cannot tell
"generating steadily, just long" from "wedged". With a 600 s ceiling and ~14 min
of honest work, every attempt was discarded seconds before it landed and retried
from scratch (vts-94wf). Silence between tokens is the signal that actually
means "stuck".

## Timeout model

Two independent limits replace the single total-duration timeout.

**Idle timeout** — the primary detector. Reset on every chunk carrying content.
Measured inter-chunk gaps are ~0.57 s, so a limit in the low minutes has a very
large margin.

**First-chunk budget** — separate and much larger, because it covers model load.
A cold model took **75 s** to load, and time-to-first-token was measured at
13.9 s and 15.6 s on a warm one. Folding this into the idle timeout would either
make the idle limit uselessly loose or fire spuriously on a cold start.

**Overall ceiling** — a backstop against generation that never ends, derived
from the work requested rather than fixed:

```
ceiling = clamp(max_tokens / min_tokens_per_second * slack,
                floor_seconds, cap_seconds)
```

Settings (YAML under `services.llm`):

| setting | meaning | default |
|---|---|---|
| `min_tokens_per_second` | below this, treat as stuck | 3 |
| `slack_multiplier` | headroom for warm-up and jitter | 1.5 |
| `floor_seconds` | lower bound, so a short request is not cut short | 300 |
| `cap_seconds` | upper bound, backstop | 3600 |
| `stream_idle_timeout_seconds` | gap between chunks | 120 |
| `stream_first_chunk_timeout_seconds` | covers model load | 300 |

Both bounds bind in practice, which is why both exist:

- segment window (1255 tokens) → `1255/3*1.5 = 628 s`; honest work is ~140–160 s
- final summary (~15000 tokens) → 7500 s → clamped down to `cap` 3600 s
- short user prompt (300 tokens) → 150 s → raised to `floor` 300 s

Measured throughput on this hardware is 9–13 tokens/s, so `min_tokens_per_second
= 3` sits well below the observed floor.

### Why not cap output with `max_tokens`

Considered and rejected as a safety limit, for two independent reasons.

It does not arrive. Sent through LiteLLM, `max_tokens: 200` produced a request
that had not finished after 10 minutes; the same limit as Ollama's native
`num_predict: 200` stopped at exactly 200 tokens (`done_reason: length`) in 66 s.
On the real segment prompt, `max_tokens: 600` returned **1070** tokens with
`finish_reason: stop`. The parameter is dropped somewhere between the proxy and
Ollama — the same class of silent loss as `cache_prompt` (vts-4jsi).

And where it *is* honoured, it truncates mid-word: the native-limit run ended
`"...увидел птиц. Леген"`. A hard token cap enforces a byte count, not a finished
summary.

`max_tokens` stays in the payload — it is the input to the ceiling formula, and
if the proxy ever forwards it, it becomes a real limit too.

## What changes in `chat_completion`

The payload-fallback queue is preserved as-is. Its eight variants
(`default`, `without_response_format`, `server_model:*`, …) exist to find a
request shape the backend accepts, and every one of those failures — HTTP 400,
unknown model, rejected `response_format` — happens **before** generation
starts. Once the first chunk arrives, the shape was accepted and the queue has
done its job:

```
for each payload variant:
    open the stream
    error before the first chunk (400, unknown model)
        → try the next variant          [unchanged behaviour]
    first chunk arrives
        → variant accepted; drain the stream
        → any error from here on fails the request,
          with no fallback and no retry
```

Mechanically: `client.post(...)` becomes `client.stream("POST", ...)` with
`"stream": true`; SSE chunks are parsed and `delta.content` accumulated; each
content chunk resets the idle timer; the ceiling is computed once before the
request; progress is logged periodically; the joined buffer is returned.

**The contract does not change.** The signature and the `str` return value stay
as they are, so the four call sites in `vts/pipeline/steps/summarization.py`
(lines 522, 837, 1105, 1336) are untouched.

**Cancellation falls out of this.** Under `atask.cancel()` the `await` inside
the stream raises `CancelledError`, httpx closes the connection, LiteLLM closes
upstream, and Ollama stops on its next chunk write.

## Error handling

| situation | behaviour |
|---|---|
| HTTP 400 / unknown model, before first chunk | next payload variant (unchanged) |
| `ConnectError`, `NetworkError`, `ProtocolError` | retry — a genuine transient |
| no first chunk within its budget | fail, **no retry**, log the wait |
| idle longer than `stream_idle_timeout_seconds` | fail, **no retry**, log tokens received |
| overall ceiling exceeded | fail, **no retry**, log tokens and tokens/s |
| stream breaks mid-generation | fail, no retry |

The "no retry on timeout" rule is the point of vts-94wf: the prompt is identical
byte-for-byte on every attempt, so a request that could not finish once will not
finish on the second or third try — it only burns the GPU. A network error is
different in kind: when Ollama was restarted mid-window on 2026-08-19 the task
failed with `ConnectError` after exhausting its attempts, and there retrying was
the right behaviour.

Every timeout logs what it saw — tokens received, elapsed, rate. That is
precisely what was missing when a retry loop was indistinguishable from a hang.

## Testing

Unit tests drive a fake SSE stream, so none of this needs a live model:

- a normal stream accumulates to the expected text
- silence longer than the idle timeout fails, and does **not** retry
- exceeding the ceiling fails, and does **not** retry
- an error before the first chunk advances to the next payload variant
- an error after the first chunk does **not** fall back and does **not** retry
- a slow but steady stream (gaps under the limit) runs past the old total
  timeout and still succeeds — the regression that motivates this work
- `ConnectError` still retries

Ceiling arithmetic is tested directly at the boundaries, including that `floor`
and `cap` each bind for the segment/final/short-prompt cases above.

One integration check against a live model, verifying that cancelling the task
closes the Ollama request promptly (the manual measurement above, automated).

## Known backend gaps

Both are silent losses between LiteLLM and Ollama, both out of scope here:

- **`max_tokens` is not forwarded** (evidence above). Tracked with vts-4jsi.
- **`cache_prompt` is rejected** — Ollama logs
  `invalid option provided option=cache_prompt`. vts-4jsi.

Worth noting for whoever picks those up: LiteLLM's `/model/update` API returns
`200` while silently not persisting `model_info` for models absent from its
built-in registry. Both times `max_input_tokens` had to be written straight to
`LiteLLM_ProxyModelTable`, after which the proxy picked it up on its next
background sync (~40 s).
