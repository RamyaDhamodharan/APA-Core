from sqlalchemy import event

from src.models.invoice_models import Invoice, _set_invoice_number_key


def test_invoice_normalization_is_registered_for_all_orm_insert_and_update_paths():
    assert event.contains(Invoice, "before_insert", _set_invoice_number_key)
    assert event.contains(Invoice, "before_update", _set_invoice_number_key)

    invoice = Invoice(invoice_no="INV-001")
    _set_invoice_number_key(None, None, invoice)
    assert invoice.invoice_no_normalized == "INV001"
