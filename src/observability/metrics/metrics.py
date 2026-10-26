# Observability metrics interface for AP Core service
from typing import Dict, Any


def get_system_metrics() -> Dict[str, Any]:
    return {
        "service": "ap-core",
        "status": "healthy",
    }
