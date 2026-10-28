"""
Configuration for field-level confidence scoring, signal weights, and threshold gating.
All weights, hard caps, and thresholds are centralized here for easy tuning.
"""

from decimal import Decimal
import os
from pydantic import BaseModel, Field


class ConfidenceConfig(BaseModel):
    # Minimum required confidence score (0-100) to bypass human verification
    threshold: float = Field(
        default_factory=lambda: float(os.getenv("FIELD_CONFIDENCE_MIN", "85"))
    )

    # Signal Weights (Must sum to 1.00)
    weight_ocr: float = Field(
        default_factory=lambda: float(os.getenv("CONFIDENCE_WEIGHT_OCR", "0.35"))
    )
    weight_consistency: float = Field(
        default_factory=lambda: float(os.getenv("CONFIDENCE_WEIGHT_CONSISTENCY", "0.25"))
    )
    weight_math: float = Field(
        default_factory=lambda: float(os.getenv("CONFIDENCE_WEIGHT_MATH", "0.20"))
    )
    weight_master_data: float = Field(
        default_factory=lambda: float(os.getenv("CONFIDENCE_WEIGHT_MASTER_DATA", "0.10"))
    )
    weight_format: float = Field(
        default_factory=lambda: float(os.getenv("CONFIDENCE_WEIGHT_FORMAT", "0.10"))
    )

    # Hard Caps - Failed checks cannot be averaged away
    cap_format_invalid: float = Field(
        default_factory=lambda: float(os.getenv("CAP_FORMAT_INVALID", "40"))
    )
    cap_math_fail: float = Field(
        default_factory=lambda: float(os.getenv("CAP_MATH_FAIL", "60"))
    )
    cap_vendor_missing: float = Field(
        default_factory=lambda: float(os.getenv("CAP_VENDOR_MISSING", "50"))
    )

    # Low quality / blurry image penalty (deducted from composite score)
    blurry_image_penalty: float = Field(
        default_factory=lambda: float(os.getenv("BLURRY_IMAGE_PENALTY", "30"))
    )

    # Valid Chart of Accounts GL codes
    valid_gl_codes: tuple[str, ...] = (
        "5000",  # Cost of Goods Sold
        "5100",  # Freight & Shipping
        "6000",  # Operating Expenses
        "6100",  # Office Supplies & Materials
        "6200",  # Software & Technology
        "6300",  # Professional Services
        "6400",  # Marketing & Advertising
        "6500",  # Facilities & Utilities
        "7000",  # Travel & Entertainment
    )


# Global singleton instance
CONFIDENCE_CONFIG = ConfidenceConfig()
