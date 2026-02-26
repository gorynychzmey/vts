FROM python:3.14-slim AS runtime

ARG VTS_VERSION=0.0.0
LABEL org.opencontainers.image.title="vts-webapi"
LABEL org.opencontainers.image.version="${VTS_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8080
CMD ["uvicorn", "vts.api.main:app", "--host", "0.0.0.0", "--port", "8080"]

