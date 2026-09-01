import hashlib
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def backfill_legacy_treasury_expenses(apps, schema_editor):
    TreasuryTransaction = apps.get_model('base', 'TreasuryTransaction')
    Expense = apps.get_model('hr', 'Expense')
    ExpenseCategory = apps.get_model('hr', 'ExpenseCategory')
    ExpenseTransition = apps.get_model('hr', 'ExpenseTransition')

    category_map = {}
    rows = TreasuryTransaction.objects.filter(
        type='EXPENSE',
        expense_payment__isnull=True,
    ).select_related('account', 'canonical_category', 'performed_by').order_by('id')
    for row in rows.iterator():
        fee = row.fee or Decimal('0')
        principal = -row.delta - fee
        if principal <= 0 or principal > Decimal('9999999999'):
            continue
        category = row.canonical_category
        historical_name = (
            row.category_name_snapshot or row.category or ''
        ).strip()
        if category is None and historical_name:
            if historical_name not in category_map:
                digest = hashlib.sha256(
                    historical_name.encode('utf-8')
                ).hexdigest()[:16].upper()
                code = f'MIGRATED_TREASURY_{digest}'
                category, _ = ExpenseCategory.objects.get_or_create(
                    code=code,
                    defaults={
                        'name': f'[Migrated Treasury] {historical_name}'[:100],
                        'description': (
                            'Isolated deterministic migration from the legacy '
                            'Treasury category string; review before merging.'
                        ),
                        'reporting_group': 'REVIEW',
                        'is_active': False,
                        'allowed_sources': ['SAFE', 'BANK'],
                        'branch_id': row.branch_id,
                    },
                )
                category_map[historical_name] = category
            category = category_map[historical_name]
        occurred_at = row.created_at
        local_date = (
            timezone.localtime(occurred_at).date()
            if timezone.is_aware(occurred_at) else occurred_at.date()
        )
        expense = Expense.objects.create(
            category_id=category.id if category else None,
            category_code_snapshot=category.code if category else '',
            category_name_snapshot=(
                category.name if category else historical_name
            ),
            category_allowed_sources_snapshot=(
                list(category.allowed_sources) if category else []
            ),
            amount=principal,
            description=row.description or historical_name,
            expense_date=local_date,
            payment_method=(
                'BANK_TRANSFER' if row.account.kind == 'BANK' else 'CASH'
            ),
            status='PAID',
            requested_source=row.account.kind,
            fee_uzs=fee,
            created_by_id=row.performed_by_id,
            approved_by_id=row.performed_by_id,
            paid_by_id=row.performed_by_id,
            approved_at=occurred_at,
            paid_at=occurred_at,
            treasury_transaction_id=row.id,
            branch_id=row.branch_id,
            notes='Migrated from legacy Treasury expense evidence.',
        )
        Expense.objects.filter(pk=expense.pk).update(
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        ExpenseTransition.objects.create(
            expense_id=expense.id,
            previous_status='',
            new_status='PAID',
            actor_id=row.performed_by_id,
            actor_display_snapshot=row.actor_display_snapshot,
            branch_id=row.branch_id,
            metadata={
                'legacy_treasury_transaction_id': row.id,
                'approval_evidence_missing': True,
            },
        )
        TreasuryTransaction.objects.filter(pk=row.pk).update(
            canonical_category_id=category.id if category else None,
            category_code_snapshot=category.code if category else '',
            category_name_snapshot=(
                category.name if category else historical_name
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ('cashbox', '0005_cashboxexpense_actor_display_snapshot_and_more'),
        ('hr', '0011_expense_category_allowed_sources_snapshot_and_more'),
    ]

    operations = [
        migrations.RunPython(
            backfill_legacy_treasury_expenses,
            migrations.RunPython.noop,
        ),
    ]
