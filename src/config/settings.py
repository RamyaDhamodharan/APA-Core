"""
Centralized Configuration for GenWorx Accounts Payable Core Service.
Reads typed configuration parameters from environment variables with safe defaults.
"""
import os
from pydantic import BaseModel, Field


class Settings(BaseModel):
    # Application & Environment
    app_name: str = "GenWorx AP Core Service"
    app_env: str = Field(default_factory=lambda: os.getenv("APP_ENV", "local"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    app_timezone: str = Field(default_factory=lambda: os.getenv("APP_TIMEZONE", "Asia/Kolkata"))

    # Database & Cache
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://ap_user:ap_dev_password@localhost:5433/genworx_ap",
        )
    )
    redis_url: str = Field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # Object Storage (MinIO / S3)
    minio_endpoint: str = Field(
        default_factory=lambda: os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    )
    minio_access_key: str = Field(
        default_factory=lambda: os.getenv("MINIO_ROOT_USER", os.getenv("MINIO_ACCESS_KEY", "minioadmin"))
    )
    minio_secret_key: str = Field(
        default_factory=lambda: os.getenv("MINIO_ROOT_PASSWORD", os.getenv("MINIO_SECRET_KEY", "minioadmin123"))
    )
    minio_bucket: str = Field(
        default_factory=lambda: os.getenv("MINIO_BUCKET", "ap-documents")
    )
    minio_secure: bool = Field(
        default_factory=lambda: os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")
    )

    # Authentication & Security
    jwt_secret_key: str = Field(
        default_factory=lambda: os.getenv("JWT_SECRET_KEY", "change-me")
    )
    jwt_algorithm: str = Field(
        default_factory=lambda: os.getenv("JWT_ALGORITHM", "HS256")
    )
    cors_allowed_origins: str = Field(
        default_factory=lambda: os.getenv(
            "CORS_ALLOWED_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
        )
    )

    # LLM & AI Pipeline
    openrouter_api_key: str = Field(
        default_factory=lambda: os.getenv("OPENROUTER_API_KEY", "")
    )
    openrouter_model: str = Field(
        default_factory=lambda: os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna-decisions")
    )
    groq_api_key: str = Field(
        default_factory=lambda: os.getenv("GROQ_API_KEY", "")
    )
    groq_model: str = Field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    )
    groq_fallback_model: str = Field(
        default_factory=lambda: os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
    )
    extraction_fallback_models: str = Field(
        default_factory=lambda: os.getenv("EXTRACTION_FALLBACK_MODELS", "")
    )
    matching_fallback_models: str = Field(
        default_factory=lambda: os.getenv("MATCHING_FALLBACK_MODELS", "")
    )

    # Celery & Asynchronous Worker
    use_celery: bool = Field(
        default_factory=lambda: os.getenv("USE_CELERY", "true").lower() in ("true", "1", "yes")
    )
    enable_goods_receipt: bool = Field(
        default_factory=lambda: os.getenv("ENABLE_GOODS_RECEIPT", "true").lower() in ("true", "1", "yes")
    )

    # Matching & Financial Tolerances
    field_confidence_min: float = Field(
        default_factory=lambda: float(os.getenv("FIELD_CONFIDENCE_MIN", "85"))
    )
    tax_tolerance_abs: float = Field(
        default_factory=lambda: float(os.getenv("TAX_TOLERANCE_ABS", "0.01"))
    )
    tax_tolerance_percent: float = Field(
        default_factory=lambda: float(os.getenv("TAX_TOLERANCE_PERCENT", "0.5"))
    )
    invoice_unit_price_tolerance_pct: float = Field(
        default_factory=lambda: float(os.getenv("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"))
    )
    invoice_total_tolerance_pct: float = Field(
        default_factory=lambda: float(os.getenv("INVOICE_TOTAL_TOLERANCE_PCT", "1"))
    )

    # Email Polling & Processing
    email_provider: str = Field(
        default_factory=lambda: os.getenv("EMAIL_PROVIDER", "imap")
    )
    email_poll_enabled: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_POLL_ENABLED", "true").lower() in ("true", "1", "yes")
    )
    email_poll_interval_seconds: int = Field(
        default_factory=lambda: int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))
    )
    email_no_match_auto_reject: bool = Field(
        default_factory=lambda: os.getenv("EMAIL_NO_MATCH_AUTO_REJECT", "true").lower() in ("true", "1", "yes")
    )
    imap_host: str = Field(default_factory=lambda: os.getenv("IMAP_HOST", ""))
    imap_port: int = Field(default_factory=lambda: int(os.getenv("IMAP_PORT", "993")))
    imap_user: str = Field(default_factory=lambda: os.getenv("IMAP_USER", ""))
    imap_password: str = Field(default_factory=lambda: os.getenv("IMAP_PASSWORD", ""))
    imap_folder: str = Field(default_factory=lambda: os.getenv("IMAP_FOLDER", "INBOX"))
    imap_use_ssl: bool = Field(
        default_factory=lambda: os.getenv("IMAP_USE_SSL", "true").lower() in ("true", "1", "yes")
    )

    # Outbound Mail & Drafts
    smtp_host: str = Field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = Field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = Field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = Field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from: str = Field(default_factory=lambda: os.getenv("SMTP_FROM", ""))
    smtp_use_ssl: bool = Field(
        default_factory=lambda: os.getenv("SMTP_USE_SSL", "false").lower() in ("true", "1", "yes")
    )
    test_mailbox_allowlist: str = Field(
        default_factory=lambda: os.getenv("TEST_MAILBOX_ALLOWLIST", "*")
    )

    # Document Retries & Sweep
    document_process_max_attempts: int = Field(
        default_factory=lambda: int(os.getenv("DOCUMENT_PROCESS_MAX_ATTEMPTS", "3"))
    )
    document_retry_base_seconds: int = Field(
        default_factory=lambda: int(os.getenv("DOCUMENT_RETRY_BASE_SECONDS", "1"))
    )
    document_stuck_timeout_minutes: int = Field(
        default_factory=lambda: int(os.getenv("DOCUMENT_STUCK_TIMEOUT_MINUTES", "15"))
    )
    document_stuck_sweep_interval_seconds: int = Field(
        default_factory=lambda: int(os.getenv("DOCUMENT_STUCK_SWEEP_INTERVAL_SECONDS", "300"))
    )


settings = Settings()
