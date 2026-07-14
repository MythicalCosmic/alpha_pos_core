from decimal import Decimal

from django.db import migrations, models
from django.db.models import Count, Sum


def merge_active_duplicate_accounts(apps, schema_editor):
    """Preserve all treasury money before enforcing one active row per kind.

    A historical first-use race could split movements across multiple active
    accounts. Keep the oldest account as canonical, sum every active balance
    into it, and soft-delete the extras. Ledger rows deliberately remain on
    their original account records: treasury history filters transaction rows
    by ``account__kind`` (not active account id), so reports still include all
    of them while their original balance_before/after audit chain is preserved.
    """
    Account = apps.get_model('base', 'TreasuryAccount')
    duplicate_kinds = (
        Account.objects.filter(is_deleted=False)
        .values('kind')
        .annotate(row_count=Count('id'))
        .filter(row_count__gt=1)
    )
    for group in duplicate_kinds.iterator():
        rows = list(
            Account.objects.filter(kind=group['kind'], is_deleted=False)
            .order_by('pk')
        )
        canonical, extras = rows[0], rows[1:]
        combined = (
            Account.objects.filter(pk__in=[row.pk for row in rows])
            .aggregate(total=Sum('balance'))['total']
            or Decimal('0')
        )
        Account.objects.filter(pk=canonical.pk).update(balance=combined)
        Account.objects.filter(pk__in=[row.pk for row in extras]).update(
            is_deleted=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0040_order_paid_at_index'),
    ]

    operations = [
        migrations.RunPython(
            merge_active_duplicate_accounts,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='treasuryaccount',
            constraint=models.UniqueConstraint(
                condition=models.Q(is_deleted=False),
                fields=('kind',),
                name='uniq_active_treasury_account_kind',
            ),
        ),
    ]
