from django.db import migrations
from django.db.models import Q


def _legacy_scope(value):
    return str(value or '').strip().lower() in {'', 'cloud'}


def repair_treasury_branch_scope(apps, schema_editor):
    AuditLog = apps.get_model('base', 'AuditLog')
    TreasuryTransaction = apps.get_model('base', 'TreasuryTransaction')
    Expense = apps.get_model('hr', 'Expense')
    ExpenseTransition = apps.get_model('hr', 'ExpenseTransition')

    rows = TreasuryTransaction.objects.select_related('account').order_by('id')
    for row in rows.iterator():
        old_branch = str(row.branch_id or '').strip()
        account_branch = str(row.account.branch_id or '').strip()
        if not _legacy_scope(old_branch) or _legacy_scope(account_branch):
            continue

        linked_expenses = Expense.objects.filter(
            Q(treasury_transaction_id=row.pk) | Q(treasury_reversal_id=row.pk)
        ).order_by('id')
        repaired_expense_ids = []
        for expense in linked_expenses.iterator():
            if not _legacy_scope(expense.branch_id):
                continue
            Expense.objects.filter(pk=expense.pk).update(
                branch_id=account_branch,
            )
            repaired_expense_ids.append(expense.pk)

        if repaired_expense_ids:
            transitions = ExpenseTransition.objects.filter(
                expense_id__in=repaired_expense_ids,
            ).order_by('id')
            for transition in transitions.iterator():
                if _legacy_scope(transition.branch_id):
                    ExpenseTransition.objects.filter(pk=transition.pk).update(
                        branch_id=account_branch,
                    )

        TreasuryTransaction.objects.filter(pk=row.pk).update(
            branch_id=account_branch,
        )
        AuditLog.objects.create(
            action='FINANCIAL_REPAIR',
            target_type='TreasuryTransaction',
            target_id=row.pk,
            branch_id=account_branch,
            metadata={
                'repair': 'TREASURY_BRANCH_SCOPE',
                'from_branch_id': old_branch,
                'to_branch_id': account_branch,
                'account_id': row.account_id,
                'expense_ids': repaired_expense_ids,
                'amounts_unchanged': True,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0063_paymentmethodconfig_provider_codes'),
    ]

    operations = [
        migrations.RunPython(
            repair_treasury_branch_scope,
            migrations.RunPython.noop,
        ),
    ]
