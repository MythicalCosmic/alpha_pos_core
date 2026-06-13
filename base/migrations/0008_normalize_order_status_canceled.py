from django.db import migrations


def forwards(apps, schema_editor):
    # Historic rows were written with status='CANCELLED' (double L) while the
    # Order.Status enum is 'CANCELED' (single L), so stats and shift reports
    # under-counted cancellations. Normalize the data to the enum value.
    Order = apps.get_model('base', 'Order')
    Order.objects.filter(status='CANCELLED').update(status='CANCELED')


def backwards(apps, schema_editor):
    # No-op: 'CANCELED' is the canonical value and we don't want to recreate
    # the inconsistency on a rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0007_syncqueuerecord'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
