from django.db import migrations
from django.db.models import F, OuterRef, Subquery

UNKNOWN_PRICE_MARKER = (
    "Purchase price was not provided; 0 is a temporary "
    "unknown-price placeholder to replace on the first purchase."
)
GRANTS = {
    "WAREHOUSE": ["stock.purchase_invoice.view", "stock.purchase_invoice.receive"],
    "MANAGER": [
        "stock.catalog.view",
        "stock.purchase_invoice.view",
        "stock.purchase_invoice.receive",
        "stock.purchase_invoice.correct",
    ],
}


def backfill(apps, schema_editor):
    db = schema_editor.connection.alias
    Receiving = apps.get_model("stock", "PurchaseReceiving")
    Order = apps.get_model("stock", "PurchaseOrder")
    Line = apps.get_model("stock", "PurchaseReceivingItem")
    OrderLine = apps.get_model("stock", "PurchaseOrderItem")
    SupplierItem = apps.get_model("stock", "SupplierStockItem")
    Role = apps.get_model("base", "RolePermission")
    User = apps.get_model("base", "User")
    Receiving.objects.using(db).filter(
        source_type="PURCHASE_ORDER", supplier__isnull=True
    ).update(
        supplier_id=Subquery(
            Order.objects.using(db)
            .filter(pk=OuterRef("purchase_order_id"))
            .values("supplier_id")[:1]
        ),
    )
    Line.objects.using(db).filter(supplier_stock_item__isnull=True).update(
        supplier_stock_item_id=Subquery(
            OrderLine.objects.using(db)
            .filter(pk=OuterRef("po_item_id"))
            .values("supplier_stock_item_id")[:1]
        ),
    )
    # No price, note, balance, quantity, or ledger value is rewritten. A zero
    # cannot become a suggested normal price; the exact import marker is also
    # authoritative evidence of unknown price, independently of numeric value.
    SupplierItem.objects.using(db).filter(price__gt=0).exclude(
        notes__contains=UNKNOWN_PRICE_MARKER
    ).update(
        price_is_known=True,
        price_source="CATALOG",
    )
    SupplierItem.objects.using(db).filter(notes__contains=UNKNOWN_PRICE_MARKER).update(
        price_is_known=False,
        price_source="IMPORT_UNKNOWN",
    )
    for role, additions in GRANTS.items():
        template, _ = Role.objects.using(db).get_or_create(
            role=role, defaults={"permissions": []}
        )
        current = template.permissions if isinstance(template.permissions, list) else []
        template.permissions = list(dict.fromkeys([*current, *additions]))
        template.save(using=db, update_fields=["permissions", "updated_at"])
        for user in (
            User.objects.using(db).filter(role=role, is_deleted=False).iterator()
        ):
            current = user.permissions if isinstance(user.permissions, list) else []
            updated = list(dict.fromkeys([*current, *additions]))
            if updated != current:
                User.objects.using(db).filter(pk=user.pk).update(
                    permissions=updated,
                    sync_version=F("sync_version") + 1,
                    synced_at=None,
                )


class Migration(migrations.Migration):
    dependencies = [
        ("stock", "0018_direct_supplier_invoices"),
        ("base", "0065_direct_supplier_invoices"),
    ]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
