FROM python:3.11-slim

# g++ is torch.compile's backend: inductor generates C++ and shells out to a
# compiler at runtime. Without it compile silently degrades to bf16-only, which
# costs half the speedup (3.62x -> 1.83x measured on a 49-min meeting).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the CPU build of torch BEFORE pyannote. pyannote pulls torch
# transitively, and the default index ships the CUDA build — ~3.4 GB of nvidia/
# and triton libraries that never run on this AMD host (no working GPU path for
# pyannote on gfx1103; see the diarization design notes). Pinning the CPU wheels
# of the exact same versions first means pip finds them already satisfied and
# skips the CUDA variant. There is no GPU to lose: switching to CUDA is a
# separate change gated on adding an NVIDIA host.
# torchcodec ships a native lib linked against a specific torch build, so it
# must come from the CPU index too — a CUDA-linked torchcodec fails to load its
# .so against a CPU torch (libtorchcodec_core*.so: cannot load).
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.13.0 torchaudio==2.11.0 torchcodec==0.15.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Weights land at build time, verified by hash: the runtime never talks to
# Hugging Face, which is what keeps the deployment on-prem and reproducible.
# A hash mismatch fails the build rather than shipping unknown weights.
COPY fetch_models.py .
RUN python fetch_models.py /models

# pyannote otherwise phones home for model resolution and telemetry.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    MODEL_DIR=/models

COPY server.py .
EXPOSE 9100
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9100"]
