# LLM backends

vts talks to a single OpenAI-compatible HTTP endpoint for summarization. The exact URL is
set by `services.llm.url` in `config.yaml` (or `VTS_LLM_URL`). However vts uses a few
endpoints **beyond** the OpenAI standard, which means not every backend works
out of the box.

## Endpoints vts uses

| Endpoint | Used for | OpenAI standard? |
|---|---|---|
| `POST /v1/chat/completions` | Generating each summary stage | Yes |
| `GET /v1/models` | Resolving the configured model name | Yes |
| `GET /props` | Reading the model's `n_ctx` so token budgets fit the loaded context | **No** — llama.cpp specific |
| `POST /tokenize` | Counting tokens precisely before each request | **No** — llama.cpp specific |
| `POST /detokenize` | Splitting and rejoining text on token boundaries | **No** — llama.cpp specific |

## Compatibility matrix

| Backend | `/chat/completions` | `/props` | `/tokenize` + `/detokenize` | Works as-is? |
|---|---|---|---|---|
| **llama.cpp server** | yes | yes | yes | ✅ Yes — native target |
| **Ollama** | yes (via `/v1/`) | no | no | ⚠️ Needs local tokenizer + manual `n_ctx` |
| **vLLM** | yes | no | no | ⚠️ Same as Ollama |
| **OpenAI / Anthropic** | yes | no | no | ⚠️ Same; also paid |
| **LiteLLM proxy** | yes | depends on backend | depends on backend | Depends — useful as a router |

The shipped prompts are tuned for the Option A setup; llama.cpp (Option C) is
the API vts was implemented against and needs no extra settings.

## Option A: LiteLLM (recommended)

The shipped prompts in `./prompts/` are tuned for **Qwen 3.6 35B**
(`qwen3.6:35b`) served over an OpenAI-compatible proxy such as
[LiteLLM](https://github.com/BerriAI/litellm). LiteLLM routes to any backend
(local Ollama/vLLM, or hosted OpenAI / Anthropic / Mistral), so it is also
the way to point vts at a hosted model.

LiteLLM does not implement the llama.cpp-only endpoints, so both caveats below
apply:

1. **Token counting falls back to a local tokenizer file.** Without it, vts
   calls `/tokenize` on every request, gets a 404, and the run breaks. Mount a
   HuggingFace `tokenizer.json` for the model and point vts at it.

2. **`n_ctx` is not auto-detected.** vts normally reads it from `/props`. Set
   it explicitly instead.

```yaml
services:
  llm:
    url: http://litellm:4000/v1
    model: qwen3.6:35b
    api_key: sk-anything       # LiteLLM accepts any non-empty bearer
    tokenizer_path: /path/to/tokenizer.json   # HF tokenizer.json for the model
    temperature: 0.15
    min_p: 0.05
summary:
  n_ctx: 32768   # match what the underlying model was launched with
```

LiteLLM is not bundled in the default `docker-compose.yml`. A minimal
addition:

```yaml
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    command: ["--config", "/app/litellm_config.yaml", "--port", "4000"]
    volumes:
      - ./litellm_config.yaml:/app/litellm_config.yaml:ro
    ports:
      - "4000:4000"
```

## Option B: Ollama

Ollama speaks OpenAI-compatible `/v1` but not the llama.cpp endpoints, so it
needs the same two settings as Option A:

```yaml
services:
  llm:
    url: http://ollama:11434/v1
    model: qwen3.5:9b
    tokenizer_path: /path/to/tokenizer.json   # HF tokenizer.json for the model
summary:
  n_ctx: 32768  # match what your Ollama model was launched with
```

After starting the stack, pull the model:

```bash
docker compose --profile llm-ollama up -d
docker compose exec ollama ollama pull qwen3.5:9b
```

## Option C: llama.cpp

The API vts is implemented against. One container, one `.gguf` file, no
tokenizer file or n_ctx wrangling needed (vts reads them from `/props` and
`/tokenize`). The default `docker-compose.yml` ships with a Qwen2.5-7B
example because Qwen3.x GGUFs are not (yet) publicly distributed; expect
summary quality to differ from the Qwen3.6 35B path the prompts are tuned
for.

```bash
mkdir -p models
# Download a model, e.g. Qwen2.5-7B-Instruct (Q4_K_M ≈ 4.6 GB):
#   https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF
# Place it at ./models/Qwen2.5-7B-Instruct-Q4_K_M.gguf

docker compose --profile llm-llamacpp --profile asr-whisper up -d
```

`config.yaml`:

```yaml
services:
  llm:
    url: http://llama:8000/v1
    model: Qwen2.5-7B-Instruct-Q4_K_M
```

## Why these llama.cpp endpoints matter

vts uses **adaptive token budgeting**: every summarization stage computes how much
output to ask for as a fraction of the input token count, clamped to fit the
remaining context window. This requires:

- An exact `n_ctx` — to know the budget ceiling.
- Exact tokenization — to measure inputs without overshooting.

When these are unavailable (Ollama / OpenAI / vLLM without a side-channel),
vts falls back to a local HuggingFace tokenizer and a configured `n_ctx`. The
output quality is the same; the difference is operational — you have to keep
those two settings in sync with the actual deployed model yourself.

## Streaming timeouts

Completions are streamed, so closing the connection stops generation on the
backend rather than leaving it to finish into a dead socket.

Three limits guard a request, each answering a different question:

| YAML key (under `services.llm`) | default | meaning |
|---|---|---|
| `stream_first_chunk_timeout_seconds` | 300 | how long to wait for the first token — this covers model load, which is slow but healthy |
| `stream_idle_timeout_seconds` | 120 | the real stall detector: silence between tokens once text is flowing |
| `min_tokens_per_second` | 3 | slowest rate still considered progress |
| `ceiling_slack_multiplier` | 1.5 | headroom in the overall ceiling |
| `ceiling_floor_seconds` | 300 | lower bound on the ceiling |
| `ceiling_cap_seconds` | 3600 | upper bound on the ceiling |

The overall ceiling is `clamp(max_tokens / min_tokens_per_second * slack,
floor, cap)`. It scales with the work requested because a segment window
(~1255 tokens) and a final summary (~15000) differ by an order of magnitude;
one constant cannot serve both.

Neither timeout is retried. The prompt is byte-identical on every attempt, so
a request that could not finish once will not finish on the second try — it
only occupies the GPU.

### Socket-level read timeout

The three limits above are all application-level: they are only evaluated
when a line arrives from the stream. If a backend accepts the connection and
then sends nothing at all, none of them ever fires — `async for` on the
response body simply never yields. The HTTP client's own socket read timeout
is what breaks that deadlock, and it is set independently of the limits
above:

```
read_timeout = max(stream_first_chunk_timeout_seconds, stream_idle_timeout_seconds)
```

This is deliberately **not** `llm_chat_timeout_seconds` — that setting stays
on connect/write/pool only and is never inherited by the streaming read. A
per-read timeout equal to the whole-request budget would multiply across
every payload-variant retry in the fallback queue (an 1800s budget across
several variants could stall for hours). If you raise
`stream_first_chunk_timeout_seconds` or `stream_idle_timeout_seconds` to
tolerate a slower backend, the socket read timeout grows with them
automatically — there is nothing extra to configure — but a stuck-but-silent
connection will still only be caught after `max()` of the two, not sooner.
