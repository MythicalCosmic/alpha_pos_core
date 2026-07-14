from django.conf import settings
from django.db import migrations
from django.db.models import F
from django.utils import timezone


def repair_legacy_branch_ownership(apps, schema_editor):
    """Repair transactional children whose old cloud ingest lost ownership.

    Older receivers stamped some server-created placeholders and settlement
    rows with the cloud node's branch.  Ownership is only changed when a
    transactional parent provides one unambiguous non-placeholder branch.
    Ambiguous and unused customers are deliberately left untouched.
    """
    database = schema_editor.connection.alias
    Customer = apps.get_model('base', 'Customer')
    Order = apps.get_model('base', 'Order')
    ShiftPaymentTotal = apps.get_model('cashbox', 'ShiftPaymentTotal')
    repaired_at = timezone.now()

    placeholder_branches = {''}
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud':
        node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if node_branch:
            placeholder_branches.add(node_branch)

    customers = Customer.objects.using(database).filter(
        branch_id__in=placeholder_branches,
    )
    for customer_id in customers.values_list('pk', flat=True).iterator(
        chunk_size=500,
    ):
        owned_branches = list(
            Order.objects.using(database)
            .filter(customer_id=customer_id)
            .exclude(branch_id__in=placeholder_branches)
            .exclude(branch_id='')
            .values_list('branch_id', flat=True)
            .distinct()[:2]
        )
        if len(owned_branches) == 1:
            Customer.objects.using(database).filter(pk=customer_id).update(
                branch_id=owned_branches[0],
                sync_version=F('sync_version') + 1,
                synced_at=None,
                updated_at=repaired_at,
            )

    totals = (
        ShiftPaymentTotal.objects.using(database)
        .values_list('pk', 'branch_id', 'shift__branch_id')
        .iterator(chunk_size=500)
    )
    for row_id, current_branch, shift_branch in totals:
        shift_branch = str(shift_branch or '').strip()
        if shift_branch and current_branch != shift_branch:
            ShiftPaymentTotal.objects.using(database).filter(pk=row_id).update(
                branch_id=shift_branch,
                sync_version=F('sync_version') + 1,
                synced_at=None,
                updated_at=repaired_at,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0044_unique_live_active_shift'),
        ('cashbox', '0002_expense_register_command'),
    ]

    operations = [
        migrations.RunPython(
            repair_legacy_branch_ownership,
            migrations.RunPython.noop,
        ),
    ]
