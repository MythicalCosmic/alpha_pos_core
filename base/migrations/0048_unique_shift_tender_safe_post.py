from datetime import timedelta
from decimal import Decimal

from django.db import migrations, models
from django.db.models import F, Q


def enable_in_progress_shifts(apps, schema_editor):
    Shift = apps.get_model('base', 'Shift')
    Inkassa = apps.get_model('base', 'Inkassa')
    TreasuryTransaction = apps.get_model('base', 'TreasuryTransaction')
    refund_prefix = (
        '[ALPHAPOS_REGISTER_COMMAND_V1]\n'
        '[ALPHAPOS_REFUND_CASH_COMMAND_V1]'
    )
    # An ACTIVE row is safe only when no legacy collection overlaps its start
    # and no settlement posting already references it. This generic preflight
    # keeps upgrades safe on databases unlike the current zero-Inkassa prod.
    for shift in Shift.objects.filter(status='ACTIVE').iterator():
        overlapping_inkassa = (
            Inkassa.objects.filter(branch_id=shift.branch_id)
            .filter(
                Q(period_end__gte=shift.start_time)
                | Q(created_at__gte=shift.start_time)
            )
            .exclude(notes__startswith=refund_prefix)
            .exists()
        )
        prior_post = TreasuryTransaction.objects.filter(
            type='SHIFT_DEPOSIT',
            reference_type='ShiftSettlement',
            reference_id=shift.id,
        ).exists()
        if not overlapping_inkassa and not prior_post:
            Shift.objects.filter(pk=shift.pk).update(
                treasury_settlement_eligible=True,
            )


def grandfather_legacy_inkassa(apps, schema_editor):
    Inkassa = apps.get_model('base', 'Inkassa')
    TreasuryTransaction = apps.get_model('base', 'TreasuryTransaction')
    # Only rows proven by an old treasury posting are grandfathered. Legacy
    # mixed batches referenced their first Inkassa row only. ``period_end`` was
    # auto_now_add, so sibling rows are not byte-identical there; identify the
    # batch by its shared period_start plus a tight creation window, then prove
    # its CASH/other split against the old SAFE/BANK ledger deltas. An unmatched
    # or ambiguous synced/local row is not proof and remains unstamped.
    refund_prefix = (
        '[ALPHAPOS_REGISTER_COMMAND_V1]\n'
        '[ALPHAPOS_REFUND_CASH_COMMAND_V1]'
    )
    referenced_ids = set(TreasuryTransaction.objects.filter(
        type='INKASSA',
        reference_type='Inkassa',
        reference_id__isnull=False,
    ).values_list('reference_id', flat=True))
    for reference_id in referenced_ids:
        first = Inkassa.objects.filter(pk=reference_id).exclude(
            notes__startswith=refund_prefix,
        ).first()
        if first is None:
            continue
        sibling_qs = Inkassa.objects.filter(
            branch_id=first.branch_id,
            cashier_id=first.cashier_id,
            period_start=first.period_start,
            created_at__gte=first.created_at - timedelta(seconds=10),
            created_at__lte=first.created_at + timedelta(seconds=10),
            treasury_allocated_at__isnull=True,
        ).exclude(notes__startswith=refund_prefix)
        siblings = list(sibling_qs)
        if not siblings:
            continue

        transactions = list(
            TreasuryTransaction.objects.filter(
                type='INKASSA',
                reference_type='Inkassa',
                reference_id=reference_id,
            ).select_related('account')
        )
        if not transactions or any(
            row.delta < 0 or row.account.kind not in ('SAFE', 'BANK')
            for row in transactions
        ):
            continue

        cash_total = sum(
            (row.amount for row in siblings if row.inkass_type == 'CASH'),
            Decimal('0.00'),
        )
        noncash_total = sum(
            (row.amount for row in siblings if row.inkass_type != 'CASH'),
            Decimal('0.00'),
        )
        safe_delta = sum(
            (row.delta for row in transactions if row.account.kind == 'SAFE'),
            Decimal('0.00'),
        )
        bank_delta = sum(
            (row.delta for row in transactions if row.account.kind == 'BANK'),
            Decimal('0.00'),
        )
        if safe_delta != cash_total or bank_delta != noncash_total:
            continue

        sibling_qs.update(
            legacy_treasury_amount=F('amount'),
            treasury_allocated_at=F('created_at'),
        )


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0047_order_delivery_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='shift',
            name='treasury_settlement_eligible',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='shift',
            name='settlement_manifest',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(
            enable_in_progress_shifts,
            migrations.RunPython.noop,
        ),
        migrations.AddField(
            model_name='cashreconciliation',
            name='treasury_posted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='collection_batch_key',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='collection_payload_hash',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='legacy_treasury_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='settlement_offset_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='inkassa',
            name='treasury_allocated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='inkassa',
            name='inkass_type',
            field=models.CharField(
                choices=[
                    ('CASH', 'Cash'), ('UZCARD', 'Uzcard'),
                    ('HUMO', 'Humo'), ('CARD', 'Card'), ('PAYME', 'Payme'),
                ],
                max_length=10,
            ),
        ),
        migrations.RunPython(
            grandfather_legacy_inkassa,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='inkassa',
            constraint=models.UniqueConstraint(
                condition=~models.Q(collection_batch_key=''),
                fields=('branch_id', 'collection_batch_key', 'inkass_type'),
                name='uniq_inkassa_branch_batch_tender',
            ),
        ),
        migrations.AddConstraint(
            model_name='inkassa',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(treasury_allocated_at__isnull=True)
                    | models.Q(
                        amount=(
                            models.F('settlement_offset_amount')
                            + models.F('legacy_treasury_amount')
                        )
                    )
                ),
                name='inkassa_allocated_amount_reconciles',
            ),
        ),
        migrations.AddConstraint(
            model_name='inkassa',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(settlement_offset_amount__gte=0)
                    & models.Q(legacy_treasury_amount__gte=0)
                ),
                name='inkassa_allocations_nonnegative',
            ),
        ),
        migrations.AddConstraint(
            model_name='treasurytransaction',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(type='SHIFT_DEPOSIT')
                    & models.Q(reference_type='ShiftSettlement')
                ),
                fields=('reference_id', 'category'),
                name='uniq_shift_tender_safe_post',
            ),
        ),
        migrations.AddConstraint(
            model_name='treasurytransaction',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(type='INKASSA')
                    & models.Q(reference_type='InkassaLegacy')
                ),
                fields=('reference_id',),
                name='uniq_legacy_inkassa_safe_post',
            ),
        ),
    ]
