from fastapi.testclient import TestClient

# Example test — proves pytest + FastAPI TestClient are wired against main.py's
# /health route. Replace with tests for your own endpoints.


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_foreign_host(client: TestClient) -> None:
    """DNS rebinding guard (CBN-248): an unrecognized Host header is refused."""
    response = client.get("/health", headers={"host": "evil.example"})
    assert response.status_code == 400
