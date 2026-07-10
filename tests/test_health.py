"""Health endpoint behavior."""


def test_health_reports_ok_with_reachable_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
