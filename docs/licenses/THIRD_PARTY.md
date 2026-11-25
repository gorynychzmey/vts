# Third-party license audit

Scope: everything VTS depends on, ships, or tells the operator to run, assessed
for **selling VTS on-premises** (fixed installation fee plus a support
subscription). Audit date: 2026-11-25. This is an engineering inventory, not
legal advice. Items marked **UNVERIFIED** need the stated check before they can
be relied on.

## How to read this document

**Who redistributes decides the obligation.** Every component falls into one
of three groups:

| Group | Meaning | Who carries the redistribution obligations |
|---|---|---|
| **Built** | Inside an image this repository builds (`docker/vts.Dockerfile`, `docker/diarization/Dockerfile`) | The vendor, whenever an image is handed to a customer |
| **Referenced** | Named in `docker-compose.yml`, pulled from a public registry at install time | The operator if the customer pulls it; **the vendor** if the vendor pulls it, mirrors it, or delivers it on an air-gapped medium |
| **Operator-supplied** | Model weights and backends VTS only talks to over HTTP (LLM, embeddings, ASR model) | The operator, unless the vendor pre-installs them, in which case the vendor |

The obligations column assumes the vendor delivers images (the realistic case
for an on-prem install). Risk is rated for the business model above:
**none / low / medium / high**.

How licenses were determined:

- Python packages: `*.dist-info/METADATA` and bundled `LICENSE` files of the
  resolved environment (`requirements.txt` installed into a virtualenv).
- Diarization image: a locally built image of `docker/diarization` was
  inspected (`importlib.metadata`, `dpkg`, `ldd`, `ffmpeg -buildconf`).
- The main `vts` image was **not** inspected as a built artefact; its
  contents are inferred from the Dockerfile and the same Debian packages.
- Models: official model cards (URLs cited inline).
- Upstream projects not installed here (whisper.cpp, llama.cpp, Ollama,
  faster-whisper, WhisperX, Deno, Valkey, pgvector): the repository's license
  file, via the GitHub license API / raw `LICENSE`, on the audit date.

---

## 1. Runtime Python dependencies (`requirements.txt`, Built into the `vts` image)

Versions with `==` are pinned; ranged ones are "resolved at build" and the
observed version is given.

### 1.1 Direct dependencies

| Component | Version | License | Obligations (on-prem sale) | Risk |
|---|---|---|---|---|
| fastapi | 0.136.1 | MIT | Keep copyright/license text | none |
| uvicorn[standard] | 0.35.0 | BSD-3-Clause | Keep copyright/license text | none |
| sqlalchemy[asyncio] | 2.0.44 | MIT | Keep copyright/license text | none |
| pgvector (Python) | 0.5.0 | MIT | Keep copyright/license text | none |
| asyncpg | 0.31.0 | Apache-2.0 | Keep license; keep NOTICE if present | none |
| alembic | 1.16.5 | MIT | Keep copyright/license text | none |
| redis[hiredis] (client) | 6.4.0 | MIT (hiredis: MIT) | Keep copyright/license text. Client only, not the Redis server (see 2.3) | none |
| httpx | 0.28.1 | BSD-3-Clause | Keep copyright/license text | none |
| pydantic / pydantic-settings | resolved, observed 2.14.4 / 2.16.3 | MIT | Keep copyright/license text | none |
| python-multipart | 0.0.32 | Apache-2.0 | Keep license | none |
| PyYAML | 6.0.3 | MIT | Keep copyright/license text | none |
| pywebpush | 2.3.0 | **MPL-2.0** | File-level copyleft: unmodified use is fine; state where its source is available; modifications to its files must be published under MPL-2.0 | low |
| tokenizers | resolved, observed 0.23.3 | Apache-2.0 | Keep license | none |
| yt-dlp | 2026.10.26 | Unlicense (public-domain dedication) | None for the wheel. See section 6 for the terms-of-service question | low (license) / medium (ToS, see 6) |
| certifi | resolved, observed 2026.10.23 | **MPL-2.0** | As pywebpush; the CA bundle data comes from Mozilla | low |
| fastmcp | resolved, observed 3.4.11 | Apache-2.0 | Keep license | none |
| authlib | resolved, observed 1.9.10 | BSD-3-Clause | Keep copyright/license text | none |
| itsdangerous | 2.2.0 | BSD-3-Clause | Keep copyright/license text | none |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause | Keep license; the wheel bundles OpenSSL (Apache-2.0) | none |
| jsonschema | 4.26.0 | MIT | Keep copyright/license text | none |

### 1.2 Transitive dependencies (observed in the resolved environment)

All permissive unless noted; obligation for all of them is "keep the copyright
and license text", which the installed `*.dist-info` directories already do.

- **MIT:** aiohttp (Apache-2.0 AND MIT), alembic, annotated-doc,
  annotated-types, anyio, attrs, beartype, cachetools, charset-normalizer,
  docstring_parser, exceptiongroup, filelock, greenlet (MIT AND PSF-2.0), h11,
  hiredis, http_ece, httptools, httpx-sse, jaraco.classes, jaraco.context,
  jaraco.functools, jeepney, jsonref, jsonschema-specifications, keyring, mako,
  markdown-it-py, mcp, mdurl, more-itertools, openapi-pydantic,
  platformdirs, pydantic_core, pyjwt, referencing, rich, rich-rst, rpds-py,
  truststore, typing-inspection, uncalled-for, urllib3, uvloop (MIT /
  Apache-2.0), watchfiles.
- **MIT-0:** cffi.
- **BSD:** click, fsspec, httpcore, httpcore2, httpx2, idna, joserfc,
  markupsafe, pycparser, pygments (BSD-2), pyperclip, python-dotenv,
  secretstorage, sse-starlette, starlette, uvicorn, websockets.
- **Apache-2.0:** aiofile, aiosignal, caio, cyclopts, fastmcp-slim,
  frozenlist, hf-xet, huggingface_hub, jsonschema-path, multidict,
  opentelemetry-api, packaging (Apache-2.0 OR BSD-2), pathable, propcache,
  py-key-value-aio, requests, yarl.
- **ISC:** dnspython, griffelib.
- **PSF-2.0:** aiohappyeyeballs, typing_extensions.
- **Unlicense:** email-validator.
- **MPL-2.0 (file-level copyleft, same handling as 1.1):** py-vapid, tqdm
  (MPL-2.0 AND MIT).

**No GPL, LGPL, AGPL, SSPL, non-commercial or unknown-license package was
found in the runtime Python environment.**

---

## 2. Container and system components

### 2.1 Main `vts` image (Built, `docker/vts.Dockerfile`)

| Component | Version | License | Obligations | Risk |
|---|---|---|---|---|
| Base `python:3.14-slim` (Debian 13 userland: glibc, bash, coreutils, apt, ...) | floating tag | Mixed: PSF-2.0 (CPython), LGPL-2.1+ (glibc), GPL-2/3 (bash, coreutils, ...), others | Redistributing the image redistributes GPL/LGPL binaries: provide (or offer in writing for 3 years) the corresponding source. In practice: record the exact Debian package versions per image and point to/mirror the matching sources (`snapshot.debian.org`, or `apt-get source`) | low (routine, but must be done) |
| **ffmpeg** (Debian package, called as a subprocess) | Debian 13 package; observed 7.1.5 in the sibling image | **GPL-2.0-or-later build** (Debian configures `--enable-gpl` with libx264, libx265, librubberband, libvidstab, libzvbi, frei0r, libcdio) | Source offer for ffmpeg and its GPL libraries, as for the base layer. VTS invokes `ffmpeg`/`ffprobe` as separate processes (`vts/pipeline/steps/media.py`, `diarization.py`, `vts/api/...`), which is aggregation: the GPL does not extend to VTS code | **medium** (obligation is real; does not affect VTS licensing) |
| curl (Debian) | Debian 13 | curl license (MIT-style) | Keep license text (already in `/usr/share/doc`) | none |
| Builder stage: build-essential, cargo, rustc | Debian 13 | GPL (gcc), MIT/Apache-2.0 (Rust) | Not present in the runtime stage, so not redistributed | none |
| Python wheels from section 1 | see 1 | see 1 | see 1 | see 1 |

**UNVERIFIED (main image ffmpeg):** the GPL configuration was observed in the
diarization image built from the same Debian release. Confirm in the built
`vts` image with:

```
ffmpeg -hide_banner -L          # prints "GNU General Public License" for a GPL build
ffmpeg -hide_banner -buildconf  # look for --enable-gpl / --enable-libx264 / --enable-nonfree
dpkg -s ffmpeg | grep Version
```

An `--enable-nonfree` build would be non-redistributable; Debian does not ship
one, but a future base-image change must be rechecked.

### 2.2 Diarization sidecar image (Built, `docker/diarization/Dockerfile`)

Inspected from a locally built image.

| Component | Version | License | Obligations | Risk |
|---|---|---|---|---|
| Base `python:3.11-slim` (Debian 13) | floating tag | Mixed, as 2.1 | Source offer for GPL/LGPL packages | low |
| ffmpeg + FFmpeg libraries (Debian) | 7:7.1.5-0+deb13u1 | **GPL-2.0-or-later build** (`--enable-gpl`, libx264, libx265, ...) | Source offer. **Linked in-process:** `torchaudio.load` goes through torchcodec, whose `libtorchcodec_core7.so` dynamically links Debian's `libavcodec.so.61`, which in turn links `libx264`/`libx265`. The sidecar process is therefore a combined work with GPL code | **medium** (high if the sidecar's own code is ever relicensed as proprietary/BSL; see section 8) |
| g++ (Debian) | Debian 13 | GPL-3.0+ with GCC Runtime Library Exception | Present at runtime (torch.compile backend): source offer | low |
| libsndfile1 | Debian 13 | LGPL-2.1+ | Source offer; dynamically linked, replaceable | low |
| torch (CPU wheel) | 2.13.0 | BSD-3-Clause plus bundled third-party (Apache-2.0, MIT, BSL-1.0, Apache-2.0 WITH LLVM-exception; bundles libgomp under the GCC Runtime Library Exception) | Keep license and the `third_party` license files shipped in the wheel's dist-info | none |
| torchaudio | 2.11.0 | BSD-2-Clause | Keep license | none |
| torchcodec | 0.15.0 | BSD-3-Clause | Keep license (but see ffmpeg row: it loads GPL-configured FFmpeg) | none (itself) |
| pyannote.audio | 4.0.7 | MIT (c) CNRS | Keep license; already credited in `docker/diarization/NOTICE` | none |
| pyannote.core / .database / .metrics / .pipeline | 6.0.1 / 6.1.1 / 4.1 / 4.0.0 | MIT (c) CNRS | Keep license | none |
| pyannoteai-sdk | 0.4.0 | MIT | Keep license | none |
| lightning, pytorch-lightning, torchmetrics, opentelemetry-*, grpcio, safetensors, sortedcontainers, huggingface_hub | observed | Apache-2.0 | Keep license | none |
| numpy, scipy, scikit-learn, pandas, matplotlib (PSF-style), networkx, sympy, joblib, contourpy, kiwisolver, protobuf, threadpoolctl, Jinja2, cycler | observed | BSD-family / PSF-style | Keep license | none |
| asteroid-filterbanks, einops, julius, optuna, torch-audiomentations, torch_pitch_shift, pytorch-metric-learning, primePy, pyparsing, colorlog, fonttools, pillow (MIT-CMU), six, setuptools, wheel | observed | MIT / MIT-CMU | Keep license | none |
| certifi, tqdm | observed | MPL-2.0 (tqdm: MPL-2.0 AND MIT) | As section 1 | low |
| Model weights in `/models` | see section 3 | CC-BY-4.0 | Attribution travels with the image (`/app/NOTICE`) | see 3 |

### 2.3 Services referenced by `docker-compose.yml` (Referenced)

| Component | Image / version | License | Obligations if the vendor redistributes | Risk |
|---|---|---|---|---|
| PostgreSQL | inside `tensorchord/vchord-postgres:pg17-v1.1.1` | PostgreSQL License | Keep license | none |
| pgvector (extension) | in the same image | PostgreSQL License | Keep license | none |
| **VectorChord (`vchord` extension)** | in the same image | **AGPL-3.0 OR Elastic License 2.0** ([README](https://github.com/tensorchord/VectorChord)) | AGPL: source offer, and network-use clause if modified. ELv2: no hosted/managed-service offering, no circumventing license keys. VTS only creates the `vector` extension (`alembic/versions/0014_pgvector_extension.py`); `vchord` is shipped but unused | **medium** (avoidable: unused component under restrictive terms) |
| **Redis server** | `redis:8` (default, overridable via `REDIS_IMAGE`) | **RSALv2 OR SSPLv1 OR AGPLv3** for Redis 8+ ([redis.io/legal/licenses](https://redis.io/legal/licenses/)); 7.2 and earlier were BSD-3 | Only the AGPLv3 option is open source; under it, unmodified redistribution needs a source offer. RSALv2 forbids commercialising or offering the functionality to third parties, which is ambiguous for a sold appliance. Separate process: no effect on VTS's own license | **medium** (compose already names Valkey as a drop-in) |
| whisper-asr-webservice | `ghcr.io/ahmetoner/whisper-asr-webservice:latest` (profile `asr-whisper`) | MIT ([repo](https://github.com/ahmetoner/whisper-asr-webservice)); engines openai-whisper (MIT), faster-whisper (MIT), WhisperX (BSD-2); PyAV bundles LGPL FFmpeg | Keep licenses; source offer for its base image's GPL/LGPL parts. The WhisperX engine downloads pyannote models with their own terms | low. **UNVERIFIED:** image is unpinned (`:latest`); inspect its ffmpeg build, base OS and any bundled CUDA libraries (NVIDIA CUDA EULA) before redistributing |
| whisper.cpp server (`whisper.backend: cpp`, supported, not in compose) | operator choice | MIT ([repo](https://github.com/ggml-org/whisper.cpp)) | Keep license | none |
| llama.cpp server | `ghcr.io/ggml-org/llama.cpp:server` (profile `llm-llamacpp`) | MIT ([repo](https://github.com/ggml-org/llama.cpp)) | Keep license | none. **UNVERIFIED:** base image contents of the tag |
| Ollama | `ollama/ollama:latest` (profile `llm-ollama`) | MIT ([repo](https://github.com/ollama/ollama)) | Keep license | low. **UNVERIFIED:** GPU variants bundle CUDA/ROCm runtime libraries under vendor EULAs; inspect before redistributing |

---

## 3. Models

None of these weights is stored in this repository. Only the diarization
weights are baked into an image this repository builds.

| Model | Where | License | Obligations | Risk |
|---|---|---|---|---|
| **pyannote `speaker-diarization-community-1`** (pipeline config, segmentation, embedding, PLDA/VBx parameters) | **Built** into the diarization image by `docker/diarization/fetch_models.py` | **CC-BY-4.0** ([official card](https://huggingface.co/pyannote/speaker-diarization-community-1)) | Attribution, license link, statement of changes (none), carried with the image (`docker/diarization/NOTICE`). Commercial use permitted by CC-BY-4.0 | low (see provenance below) |
| ↳ embedding component: WeSpeaker ResNet34 (VoxCeleb) | part of the above | CC-BY-4.0, inherited from the VoxCeleb dataset ([card](https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM)) | Credit WeSpeaker (Wang et al., 2023) | low |
| ↳ PLDA / VBx clustering parameters | part of the above | covered by the pipeline's CC-BY-4.0; trained by BUT Speech@FIT | Credit BUT Speech@FIT / VBx (Landini et al., 2022) | low |
| ↳ segmentation component | part of the above | covered by the pipeline's CC-BY-4.0 | Credit Plaquet & Bredin (2023) | low |
| Whisper models (OpenAI; also faster-whisper/CTranslate2 and ggml conversions) | Operator-supplied, downloaded by the ASR service (compose default `ASR_MODEL=medium`) | MIT, code and weights ([repo](https://github.com/openai/whisper)) | Keep license if pre-installed | none |
| `bge-m3` (default `embedding_model`) | Operator-supplied, served by the LLM gateway | MIT ([card](https://huggingface.co/BAAI/bge-m3)) | Keep license if pre-installed | none |
| LLM: `Qwen2.5-7B-Instruct` (config default) / Qwen3.5 9B (prompts tuned for it) | Operator-supplied via any OpenAI-compatible endpoint | Apache-2.0 ([2.5-7B card](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct), [3.5-9B card](https://huggingface.co/Qwen/Qwen3.5-9B)) | Keep license and NOTICE if pre-installed | none for these sizes. **Not all Qwen sizes are Apache-2.0**: e.g. Qwen2.5-72B uses the "Qwen" license ([card](https://huggingface.co/Qwen/Qwen2.5-72B-Instruct)). Check each model actually installed |
| Tokenizer file (`llm_tokenizer_path`, Ollama setups) | Operator-supplied | That of the model it belongs to | As the model | none |

**Diarization provenance (UNVERIFIED in part).** `fetch_models.py` downloads
from `pyannote-community/speaker-diarization-community-1`, an ungated mirror,
not from the official, gated `pyannote/speaker-diarization-community-1`.
Checked 2026-11-25 via the Hugging Face API: the mirror is labelled
`cc-by-4.0`, its LFS sha256 values equal the hashes pinned in
`fetch_models.py`, and every file size equals the official repo's. The official
repo's hashes are hidden behind the gate, so **byte identity with the
official release is not verified**. The gate itself collects contact details
(company, use case) and marketing consent; it is not a license restriction, and
CC-BY-4.0 permits redistribution through a mirror.

**Not a license matter, but relevant to German customers:** speaker embeddings
stored in the voice registry are biometric data under GDPR Art. 9. The
operator needs a legal basis; the product documentation should say so.

---

## 4. Frontend assets (Built into the `vts` image, served to browsers)

| Component | License | Obligations | Risk |
|---|---|---|---|
| Manrope font (`vts/static/fonts/manrope-*.woff2`) | SIL OFL-1.1, "Copyright 2018 The Manrope Project Authors" ([OFL.txt](https://github.com/google/fonts/blob/main/ofl/manrope/OFL.txt)) | Bundle the copyright notice and OFL text with the fonts; fonts may not be sold on their own (selling them as part of software is allowed) | low (license text missing, see actions) |
| IBM Plex Mono font (`vts/static/fonts/ibmplexmono-*.woff2`) | SIL OFL-1.1, "Copyright © 2017 IBM Corp. with Reserved Font Name "Plex"" ([OFL.txt](https://github.com/google/fonts/blob/main/ofl/ibmplexmono/OFL.txt)) | As Manrope. The files are Google Fonts unicode-range subsets; subsetting is a modification under OFL, and modified versions may not use the Reserved Font Name | low (see actions) |
| Icons (inline SVG in `index.html` / `app.js`), favicon, PWA icons | Project's own (MIT) as far as could be determined | None | low. **UNVERIFIED:** about 80 inline 24x24 icons; no third-party icon-set marker found in code, docs or history. Confirm with the author that none were copied from an icon set (Lucide ISC, Material Apache-2.0 etc. would need attribution) |
| Vendored JavaScript | None: `app.js`, `webmcp.js`, `sw.js`, `status-predicates.js` are project code; no third-party JS is vendored | None | none |

---

## 5. VTS's own packages

| Component | License | Notes | Risk |
|---|---|---|---|
| VTS (this repository) | MIT, (c) 2026 Viktor Vostrikov (`LICENSE`) | | none |
| `packages/vts-delivery-contract` (1.4.0) | Effectively MIT (it force-includes `vts/delivery/contract.py` and `__init__.py` from this repository) | **`pyproject.toml` declares no `license` field and ships no license file**, so the built wheel carries no license metadata | low (see actions; matters for relicensing, section 8) |

---

## 6. yt-dlp: license and terms-of-service position

- **License:** the PyPI wheel VTS installs contains only Unlicense code
  ([README](https://github.com/yt-dlp/yt-dlp)). The GPLv3+ caveat in yt-dlp's
  README applies only to its PyInstaller release executables, which VTS does
  not use. No obligations.
- **Full YouTube support** now needs the optional `yt-dlp-ejs` package
  (Unlicense, bundles MIT/ISC code) plus a JavaScript runtime such as Deno.
  VTS installs neither; if added later, their licenses (Deno: MIT) must be
  added here.
- **Terms of service:** downloading from YouTube and similar platforms
  generally violates the platform's terms and, for protected content, can
  touch copyright law (in Germany, §95a UrhG on circumventing technical
  measures). The license of yt-dlp does not change this.
  - **On-prem:** the operator runs the download from their own
    infrastructure and decides what URLs are submitted; the operator bears
    the ToS and copyright exposure.
  - **Hosted service:** the vendor would be the one downloading, and bears it.
- **Recommended positioning:** treat URL ingestion as an optional feature
  that the operator can disable (a configuration switch plus documentation),
  describe it as "fetching media the operator is entitled to access" rather
  than as a YouTube downloader, and put a clause in the customer contract that
  the operator is responsible for having the rights to every submitted URL and
  for complying with third-party terms. File upload remains the primary,
  risk-free input path. No configuration switch to disable URL ingestion was
  found in `vts/` at the time of this audit; adding one is part of finding #11.

---

## 7. Dev-only tools (not shipped)

| Component | Version | License | Note |
|---|---|---|---|
| pytest | 8.4.2 | MIT | `requirements-dev.txt` |
| pytest-asyncio | resolved, observed 0.26.0 | Apache-2.0 | `requirements-dev.txt` |
| iniconfig, pluggy | observed | MIT | pytest dependencies |
| Playwright (`tests/ui`) | `^1.40.0`, observed 1.61.1 | Apache-2.0 | Browser binaries it downloads (Chromium) are BSD-style plus third-party; never shipped |
| Builder toolchain (gcc, cargo, rustc) | Debian 13 | GPL / MIT / Apache-2.0 | Builder stage only |
| CI Postgres service | `tensorchord/vchord-postgres` | as 2.3 | Test only |

No obligations arise from dev-only tools as long as they are not shipped.

---

## 8. Findings and actions

Each item must be resolved (or consciously accepted) before selling.

| # | Finding | Risk | Action |
|---|---|---|---|
| 1 | **GPL source obligations for delivered images.** Both images contain Debian GPL/LGPL packages, including a GPL-configured ffmpeg (and g++ in the sidecar). Switching ffmpeg to an LGPL build would not remove the obligation for the rest of the base layer | medium | Set up a source-offer process per release: record package versions (`dpkg -l` per image), keep matching source (or a pinned `snapshot.debian.org` reference) available for 3 years, and include a written source offer in the delivery (draft below). Pin base images by digest so the record is reproducible |
| 2 | **Diarization sidecar links GPL FFmpeg in-process** (torchaudio → torchcodec → Debian `libavcodec`, which links libx264/libx265) | medium | Fine while the sidecar code stays MIT/AGPL (GPL-compatible) and the source offer from #1 covers FFmpeg. If the sidecar is ever to be proprietary/BSL: build or install an LGPL-only FFmpeg (no `--enable-gpl`, no libx264/libx265; decoding needs neither) for torchcodec, or keep `docker/diarization/server.py` under a permissive license regardless of the main app's license |
| 3 | **Redis 8 default is RSALv2/SSPLv1/AGPLv3** | medium | Change the compose default to Valkey (BSD-3-Clause; already documented as drop-in) or Redis ≤7.2 (BSD-3). If Redis 8 stays, elect the AGPLv3 option explicitly and include it in the source offer |
| 4 | **VectorChord (AGPL-3.0 / ELv2) shipped but unused** in the Postgres image | medium | Switch compose, CI (`.github/workflows/tests.yml`), `build.sh` and `docs/INITIAL_DEPLOYMENT.md` to a plain pgvector image (e.g. `pgvector/pgvector:pg17`, PostgreSQL License), after confirming no index or query depends on `vchord`. If kept, elect AGPLv3 and add it to the source offer; ELv2 would bar a future hosted offering |
| 5 | **Diarization weights come from an unofficial mirror**; byte identity with the official gated release is unverified | low | Accept the gated conditions once with a vendor Hugging Face account, compare the official sha256 values with those pinned in `fetch_models.py`, and record the result. Point `docker/diarization/NOTICE` at the official repository (keep the mirror as the download location if desired) |
| 6 | **CC-BY-4.0 attribution** for the diarization weights is present in the sidecar image but not visible to end users or in product docs | low | Keep `docker/diarization/NOTICE`; add the component credits (WeSpeaker, VBx/BUT Speech@FIT, pyannote segmentation) to it; reproduce the attribution in the top-level `NOTICE` (done) and in the product's "About / licenses" page or documentation |
| 7 | **Font license texts not shipped**; IBM Plex Mono subsets may conflict with the Reserved Font Name "Plex" | low | Add `vts/static/fonts/OFL-Manrope.txt` and `OFL-IBMPlexMono.txt` (copyright line + OFL-1.1). For Plex, prefer IBM's own unmodified split `woff2` files from the [IBM Plex release](https://github.com/IBM/plex), or accept the Google Fonts subsets as the common industry practice |
| 8 | **Delivery contract package has no license metadata** | low | Add `license = "MIT"` (or Apache-2.0) and `license-files` to `packages/vts-delivery-contract/pyproject.toml`. Keep it permissive regardless of the main app's future license, so third-party plugins are not pulled into AGPL/BSL terms |
| 9 | **Referenced third-party images are unpinned** (`whisper-asr-webservice:latest`, `llama.cpp:server`, `ollama:latest`) and were not inspected | low | If the vendor delivers them: pin by digest, inspect licenses (ffmpeg build, CUDA/ROCm EULAs) and add them to the source offer. Otherwise document that the operator pulls them |
| 10 | **LLM/embedding/ASR models are operator-supplied** | low | State in the contract which party supplies the models. If the vendor pre-installs a model, check its card (not every Qwen size is Apache-2.0) and ship its license/NOTICE |
| 11 | **yt-dlp terms-of-service exposure** | medium (hosted) / low (on-prem) | Position URL ingestion as an optional, disable-able feature; operator-responsibility clause in the contract (section 6) |
| 12 | **Codec patents** (H.264/H.265/AAC via ffmpeg) | low | Not a copyright-license matter; ask counsel whether decoding in a sold appliance needs patent-pool coverage in the target market. An LGPL build without x264/x265 encoders also narrows this |
| 13 | **GDPR Art. 9**: voice embeddings are biometric data | n/a (compliance) | Document for operators; not a license issue |

**Draft written source offer (finding #1).** Not yet adopted: it is a
commitment the vendor must decide to make, so it is deliberately absent from
`NOTICE`. Suggested wording for the delivery documentation:

> For at least three years after delivery, [vendor] will provide, on request
> and for no more than the cost of physically performing distribution, the
> complete corresponding source code of every GPL- or LGPL-licensed component
> contained in the delivered VTS container images. The same sources are also
> available from https://snapshot.debian.org/ for the exact package versions
> listed by `dpkg -l` inside each image.

---

## 9. Relicensing compatibility (MIT → AGPL, or BSL + commercial)

The decision is the owner's; this section only checks whether the
dependencies found allow each option.

- **Runtime Python dependencies:** all permissive or MPL-2.0. MPL-2.0 is
  file-level, compatible with AGPLv3 (via its secondary-license clause) and
  with proprietary/BSL distribution as long as the MPL files stay unmodified
  or their modifications are published. **No Python dependency blocks any of
  the three options.**
- **ffmpeg in the main image:** subprocess use, so aggregation. Does not
  block AGPL, BSL or proprietary licensing of VTS. Obligation #1 applies in
  all cases.
- **Diarization sidecar:** in-process GPL FFmpeg (finding #2). Compatible with
  MIT and AGPL (FFmpeg is GPL-2.0-or-later, usable under GPLv3, which
  AGPLv3 §13 permits combining with). **Blocks a proprietary/BSL license for
  the sidecar's own code** unless FFmpeg is replaced with an LGPL build or the
  sidecar code stays permissively licensed.
- **Redis 8 / VectorChord:** separate processes, so they do not affect VTS's
  license under any option; they affect what may be shipped (findings #3,
  #4). ELv2 (VectorChord) and RSALv2 (Redis) would conflict with a future
  hosted/managed offering under any VTS license.
- **Models:** CC-BY-4.0, MIT and Apache-2.0 impose attribution only and are
  compatible with all three options.
- **Delivery contract:** should remain permissive under every option (finding
  #8). Under AGPL, a plugin importing an AGPL contract module would arguably
  form a combined work; under BSL, plugin authors would need a grant.
- **Past releases:** versions already published under MIT stay MIT for
  whoever received them; relicensing applies to future versions only. Sole
  authorship simplifies relicensing; any external contributions need a
  contributor agreement or the contributor's consent. Git history shows a
  single human author (commits co-authored by AI assistants aside); recheck
  before relicensing.
