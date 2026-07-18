"""Sidecar contract tests. Model loading is mocked — we assert wire shape only."""
from fastapi.testclient import TestClient
import server


def test_health_reports_model(monkeypatch):
    client = TestClient(server.app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["embedding_model"] == server.EMBEDDING_MODEL_ID


def test_embed_returns_vector(monkeypatch):
    # Stub the embedding model so no weights are loaded.
    monkeypatch.setattr(server, "_embed_wav", lambda path: [0.5] * 256)
    client = TestClient(server.app)
    r = client.post("/embed", files={"file": ("a.wav", b"RIFFxxxx", "audio/wav")})
    assert r.status_code == 200
    body = r.json()
    assert len(body["embedding"]) == 256
    assert body["embedding_model"] == server.EMBEDDING_MODEL_ID
