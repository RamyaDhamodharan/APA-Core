"""
Application module for validated percentage tolerance configuration services.
Flow: load and validate financial matching tolerances at startup to prevent misconfigurations.
Called by: app.lifespan, ap_decision_engine, llm_matching_services.
Calls: none.
Tables: no database tables directly.
"""
import os
from decimal import Decimal, InvalidOperation

# Maximum allowed tolerance ceiling (2.0%)
MAX_TOLERANCE_PERCENT = Decimal("2")


def read_tolerance_percent(name: str, default: str) -> Decimal:
    """
    Read a percentage tolerance configuration value constrained to [0.0, 2.0]%.
    Inputs: name (str) - environment variable key, default (str) - default value.
    Output: Decimal - parsed percentage value.
    Effects: reads environment variables; raises ValueError if out of bounds.
    Results: guarantees strict tolerance ceilings.
    """
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number between 0 and 2") from exc
    # Enforce finite non-negative number within maximum tolerance ceiling
    if not value.is_finite() or value < 0 or value > MAX_TOLERANCE_PERCENT:
        raise ValueError(f"{name} must be between 0 and 2 percent")
    return value


def read_nonnegative_decimal(name: str, default: str) -> Decimal:
    """
    Read a non-negative Decimal configuration value (e.g. absolute tax tolerance).
    Inputs: name (str) - environment variable key, default (str) - default value.
    Output: Decimal - parsed non-negative Decimal.
    Effects: reads environment variables; raises ValueError on invalid input.
    Results: safe numerical threshold for monetary comparison.
    """
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def tolerance_fraction(name: str, default: str) -> Decimal:
    """
    Convert a percentage tolerance (e.g. 2%) into its decimal multiplier fraction (0.02).
    Inputs: name (str), default (str).
    Output: Decimal - fraction representation (e.g., Decimal('0.02')).
    Effects: pure calculation.
    Results: used directly in arithmetic multiplication against prices and totals.
    """
    return read_tolerance_percent(name, default) / Decimal(100)


def validate_tolerance_config() -> None:
    """
    Eagerly validate all matching tolerance environment variables during application startup.
    Inputs: none.
    Output: None.
    Effects: called inside FastAPI lifespan startup.
    Results: fails fast on startup if configuration contains invalid thresholds.
    """
    # Step 1: Validate percentage tolerances
    for name, default in (
        ("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"),
        ("TAX_TOLERANCE_PERCENT", "0.5"),
        ("INVOICE_TOTAL_TOLERANCE_PCT", "1"),
    ):
        read_tolerance_percent(name, default)
    # Step 2: Validate absolute monetary tolerances
    read_nonnegative_decimal("TAX_TOLERANCE_ABS", "0.01")
