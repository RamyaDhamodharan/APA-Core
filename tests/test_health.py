import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi.testclient import TestClient

CORE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(CORE_ROOT))
load_dotenv(CORE_ROOT / ".env")

from src.api.rest.app import app  # noqa: E402

client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ap-core",
    }
