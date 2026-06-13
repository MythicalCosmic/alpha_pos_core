from django.db import migrations


def forwards(apps, schema_editor):
    # Historic rows were written with status='CANCELLED' (double L) while the
    # enums on PurchaseOrder, ProductionOrder, StockTransfer, and StockCount
    # were just normalized to 'CANCELED' (single L) to match base.Order.
    # Without this any old CANCELLED row falls outside the enum and gets
    # under-counted by status filters (mirrors base/0008 for the order side).
    for label in ('PurchaseOrder', 'ProductionOrder', 'StockTransfer', 'StockCount'):
        Model = apps.get_model('stock', label)
        Model.objects.filter(status='CANCELLED').update(status='CANCELED')


def backwards(apps, schema_editor):
    # No-op: CANCELED is canonical project-wide; rolling back to CANCELLED
    # would re-introduce the spelling drift this migration exists to fix.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0002_alter_productionorder_status_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
