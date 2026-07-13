from django.conf import settings
from django.db import migrations, models


def consolidate_registers(apps, schema_editor):
    """Backfill ownership and keep the newest live register per branch.

    Historical duplicate rows cannot be summed safely: most are stale copies of
    the same running balance. ``last_updated`` identifies the row that was most
    recently used, so it becomes authoritative and the others remain as
    soft-deleted audit history.
    """
    CashRegister = apps.get_model('base', 'CashRegister')
    default_branch = str(getattr(settings, 'BRANCH_ID', '') or 'main').strip()
    CashRegister.objects.filter(branch_id='').update(branch_id=default_branch)

    branches = CashRegister.objects.filter(is_deleted=False).values_list(
        'branch_id', flat=True,
    ).distinct()
    for branch in branches.iterator():
        rows = list(
            CashRegister.objects.filter(branch_id=branch, is_deleted=False)
            .order_by('-last_updated', '-id')
            .values_list('id', flat=True)
        )
        if len(rows) > 1:
            CashRegister.objects.filter(id__in=rows[1:]).update(is_deleted=True)


class Migration(migrations.Migration):
    dependencies = [('base', '0037_payment_method_card')]

    operations = [
        migrations.RunPython(consolidate_registers, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='cashregister',
            constraint=models.UniqueConstraint(
                fields=('branch_id',),
                condition=models.Q(is_deleted=False),
                name='uniq_cash_register_active_branch',
            ),
        ),
        migrations.AddConstraint(
            model_name='cashregister',
            constraint=models.CheckConstraint(
                condition=models.Q(is_deleted=True) | ~models.Q(branch_id=''),
                name='cash_register_active_branch_required',
            ),
        ),
    ]
