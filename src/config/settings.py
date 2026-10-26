import os
from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "GenWorx AP Core Service"
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg://ap_user:ap_dev_password@localhost:5433/genworx_ap")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "change-me")
    field_confidence_min: float = float(os.getenv("FIELD_CONFIDENCE_MIN", "85"))
    invoice_unit_price_tolerance_pct: float = float(os.getenv("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"))
    invoice_total_tolerance_pct: float = float(os.getenv("INVOICE_TOTAL_TOLERANCE_PCT", "1"))


settings = Settings()
