FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
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
