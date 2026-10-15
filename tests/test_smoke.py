"""Phase 0 smoke tests: config, logging, health and ready endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def test_settings_load():
    """Config loads with defaults (no .env file needed)."""
    from incubrix.config import get_settings

    s = get_settings()
    assert s.STT_PROVIDER == "mock"
    assert s.LLM_PROVIDER == "mock"
    assert s.MIN_SPEED == 0.25
    assert s.MAX_SPEED == 4.0


def test_logging_setup():
    """Structured logging initialises without error."""
    from incubrix.logging import correlation_id_var, get_logger, setup_logging

    setup_logging(log_level="DEBUG", log_format="json")
    log = get_logger("test")
    correlation_id_var.set("test-cid-123")
    # Should not raise
    log.info("smoke_test", key="value")


def test_health_endpoint():
    """GET /health returns 200 {status: ok}."""
    from incubrix.api.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_correlation_id_echoed():
    """Correlation ID is echoed back in the response header."""
    from incubrix.api.app import app

    client = TestClient(app)
    resp = client.get("/health", headers={"X-Correlation-Id": "my-cid-456"})
    assert resp.headers["x-correlation-id"] == "my-cid-456"


def test_correlation_id_generated():
    """A correlation ID is generated when the client doesn't send one."""
    from incubrix.api.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert "x-correlation-id" in resp.headers
    assert len(resp.headers["x-correlation-id"]) > 0


def test_ready_endpoint_down():
    """GET /ready returns 503 when backing services are unreachable."""
    from incubrix.api.app import app

    with patch("sqlalchemy.create_engine", side_effect=Exception("DB connection refused")):
        with patch("boto3.client", side_effect=Exception("S3 unreachable")):
            client = TestClient(app)
            resp = client.get("/ready")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unavailable"
            assert "errors" in resp.json()
            assert len(resp.json()["errors"]) == 2


def test_ready_endpoint_up():
    """GET /ready returns 200 when backing services are reachable."""
    from incubrix.api.app import app

    with patch("sqlalchemy.create_engine") as mock_engine, patch("boto3.client") as mock_boto:
        mock_conn = MagicMock()
        mock_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3

        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}
