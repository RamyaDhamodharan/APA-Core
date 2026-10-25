from decimal import Decimal

import pytest

from src.services.tolerance_config_services import read_tolerance_percent


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"),
        ("TAX_TOLERANCE_PERCENT", "0.5"),
        ("INVOICE_TOTAL_TOLERANCE_PCT", "1"),
    ],
)
def test_tolerance_percent_accepts_two(name, default, monkeypatch):
    monkeypatch.setenv(name, "2")

    assert read_tolerance_percent(name, default) == Decimal("2")


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("INVOICE_UNIT_PRICE_TOLERANCE_PCT", "2"),
        ("TAX_TOLERANCE_PERCENT", "0.5"),
        ("INVOICE_TOTAL_TOLERANCE_PCT", "1"),
    ],
)
@pytest.mark.parametrize("value", ["2.01", "-0.01"])
def test_tolerance_percent_rejects_out_of_range(name, default, value, monkeypatch):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=f"{name} must be between 0 and 2 percent"):
        read_tolerance_percent(name, default)
