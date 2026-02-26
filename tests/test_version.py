from fastapi.testclient import TestClient

from vts import __version__
from vts.api.main import app


def test_version_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.json()["version"] == __version__

