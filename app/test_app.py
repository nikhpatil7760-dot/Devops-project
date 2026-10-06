from app import app


def test_index_renders_agentic_world_welcome_page():
    response = app.test_client().get("/")

    assert response.status_code == 200
    assert response.content_type.startswith("text/html")
    assert b"Welcome to the" in response.data
    assert b"agentic" in response.data
    assert b"world." in response.data
    assert b"LOCAL SIMULATION" in response.data
    assert b"DevOps Platform Demo" in response.data
    assert b"ENVIRONMENT" in response.data
    assert b"Version" in response.data
    assert b"KUBERNETES" in response.data


def test_health_returns_healthy_status():
    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "healthy"}


def test_index_displays_deployment_context(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("APP_VERSION", "release-42")
    monkeypatch.setenv("KUBERNETES_STATUS", "Connected")

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"staging" in response.data
    assert b"release-42" in response.data
    assert b"Connected" in response.data


def test_metrics_exposes_http_request_count():
    client = app.test_client()
    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert b"flask_http_requests_total" in response.data
    assert b'endpoint="health"' in response.data