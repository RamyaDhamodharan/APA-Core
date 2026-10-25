"""
Application module for container runtime configuration checks.
Flow: inspect environment configuration during startup to catch misconfigured localhost endpoints inside containerized stacks.
Called by: app.lifespan.
Calls: none.
Tables: no database tables directly.
"""
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_running_in_container() -> bool:
    """
    Detect whether the application process is running inside a Docker or OCI container.
    Inputs: none.
    Output: bool - True if container indicator files exist, False if running directly on host.
    Effects: checks filesystem for /.dockerenv or /run/.containerenv.
    Results: gates container-specific configuration warnings.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _is_localhost_endpoint(value: str) -> bool:
    """
    Check if a URI or hostname refers to localhost or loopback address (127.0.0.1).
    Inputs: value (str) - endpoint string (e.g. 'redis://localhost:6379/0').
    Output: bool - True if hostname resolves to localhost or loopback.
    Effects: parses URI.
    Results: identifies invalid inter-container references.
    """
    candidate = value if "://" in value else f"//{value}"
    hostname = urlparse(candidate).hostname
    return hostname == "localhost" or hostname == "::1" or (
        hostname is not None and hostname.startswith("127.")
    )


def warn_if_container_redis_url_is_localhost() -> None:
    """
    Log a visible warning if a containerized worker is configured with a localhost Redis URL.
    Inputs: none.
    Output: None.
    Effects: reads REDIS_URL and APP_ENV; logs warning if misconfigured.
    Results: alerts operators to use Docker compose service name 'redis' instead of localhost.
    """
    app_env = os.getenv("APP_ENV", "").casefold()
    if app_env == "dev" or not is_running_in_container():
        return

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    if _is_localhost_endpoint(redis_url):
        logger.warning(
            "REDIS_URL points to localhost inside a container (APP_ENV=%s); "
            "use the Redis service hostname, for example redis://redis:6379/0",
            app_env or "unset",
        )


def warn_if_container_minio_endpoint_is_localhost() -> None:
    """
    Log a visible warning if a containerized worker is configured with a localhost MinIO endpoint.
    Inputs: none.
    Output: None.
    Effects: reads MINIO_ENDPOINT and APP_ENV; logs warning if misconfigured.
    Results: alerts operators to use Docker compose service name 'minio' instead of localhost.
    """
    app_env = os.getenv("APP_ENV", "").casefold()
    if app_env == "dev" or not is_running_in_container():
        return

    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    if _is_localhost_endpoint(endpoint):
        logger.warning(
            "MINIO_ENDPOINT points to localhost inside a container (APP_ENV=%s); "
            "use the reachable MinIO service or host endpoint",
            app_env or "unset",
        )
