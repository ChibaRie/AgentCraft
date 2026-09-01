from fastapi.testclient import TestClient

from backend.main import app


def test_health_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok", "version": "0.4.0"}}
