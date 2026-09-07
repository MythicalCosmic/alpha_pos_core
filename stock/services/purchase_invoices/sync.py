"""Direct invoice commands are authored on the admin server, never by peer sync."""

from django.conf import settings
from django.core.exceptions import ValidationError


def branch_write_forbidden(model, data, *, existing=None):
    if getattr(settings, "DEPLOYMENT_MODE", "local") != "cloud":
        return False
    label = model._meta.label_lower
    parents = {
        "stock.purchaseorderitem": ("purchase_order", "purchase_order_uuid", "order"),
        "stock.purchasereceivingitem": ("receiving", "receiving_uuid", "receiving"),
        "stock.purchasereceivingcorrection": (
            "receiving",
            "receiving_uuid",
            "receiving",
        ),
    }
    if label in ("stock.purchaseorder", "stock.purchasereceiving"):
        return (
            data.get("source_type") == "DIRECT_INVOICE"
            or getattr(existing, "source_type", None) == "DIRECT_INVOICE"
        )
    if label == "stock.suppliertransaction":
        return bool(data.get("invoice_posting_id"))
    if label not in parents:
        return False
    from stock.models import PurchaseOrder, PurchaseReceiving

    field, key, kind = parents[label]
    parent = PurchaseOrder if kind == "order" else PurchaseReceiving
    if (
        existing
        and parent.objects.filter(
            pk=getattr(existing, field + "_id"), source_type="DIRECT_INVOICE"
        ).exists()
    ):
        return True
    if data.get(key):
        try:
            return parent.objects.filter(
                uuid=data[key], source_type="DIRECT_INVOICE"
            ).exists()
        except (ValidationError, ValueError):
            return False  # The ordinary FK validator handles malformed IDs.
    return False
