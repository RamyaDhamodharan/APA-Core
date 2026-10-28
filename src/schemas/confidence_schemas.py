"""
Schemas for field-level confidence scoring, signal breakdowns, and audit metadata.
"""

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class SignalBreakdown(BaseModel):
    ocr: float = Field(default=100.0, ge=0.0, le=100.0, description="OCR/extraction reader confidence")
    consistency: float = Field(default=100.0, ge=0.0, le=100.0, description="Two-pass consistency agreement")
    math: float = Field(default=100.0, ge=0.0, le=100.0, description="Cross-field arithmetic integrity")
    master_data: float = Field(default=100.0, ge=0.0, le=100.0, description="Master-data lookup validity")
    format: float = Field(default=100.0, ge=0.0, le=100.0, description="Format validity and schema conformance")
    reasons: list[str] = Field(default_factory=list, description="Explanations for penalties or caps")


class ScoredField(BaseModel):
    value: Any = None
    confidence: int = Field(default=0, ge=0, le=100, description="Final composite confidence score (0-100)")
    source_signals: SignalBreakdown = Field(default_factory=SignalBreakdown)
    edited_by: Optional[str] = None
    edited_at: Optional[datetime] = None
    original_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": int(self.confidence),
            "source_signals": self.source_signals.model_dump(),
            "edited_by": str(self.edited_by) if self.edited_by else None,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
            "original_value": self.original_value,
        }


class InvoiceConfidenceBreakdown(BaseModel):
    overall_confidence: int = 100
    requires_verification: bool = False
    lowest_field: str = ""
    fields: dict[str, ScoredField] = Field(default_factory=dict)
    line_items: list[dict[str, ScoredField]] = Field(default_factory=list)
