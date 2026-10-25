import logging

from src.services import runtime_config_services


def test_warns_when_container_redis_url_uses_localhost(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(
        runtime_config_services, "is_running_in_container", lambda: True
    )

    with caplog.at_level(logging.WARNING):
        runtime_config_services.warn_if_container_redis_url_is_localhost()

    assert "REDIS_URL points to localhost inside a container" in caplog.text
    assert "redis://redis:6379/0" in caplog.text


def test_warns_when_container_minio_endpoint_uses_localhost(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://localhost:9000")
    monkeypatch.setattr(
        runtime_config_services, "is_running_in_container", lambda: True
    )

    with caplog.at_level(logging.WARNING):
        runtime_config_services.warn_if_container_minio_endpoint_is_localhost()

    assert "MINIO_ENDPOINT points to localhost inside a container" in caplog.text


def test_does_not_warn_for_minio_localhost_in_dev(monkeypatch, caplog):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://localhost:9000")
    monkeypatch.setattr(
        runtime_config_services, "is_running_in_container", lambda: True
    )

    with caplog.at_level(logging.WARNING):
        runtime_config_services.warn_if_container_minio_endpoint_is_localhost()

    assert "MINIO_ENDPOINT" not in caplog.text
