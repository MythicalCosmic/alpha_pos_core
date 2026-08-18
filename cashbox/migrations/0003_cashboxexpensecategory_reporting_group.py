from django.db import migrations, models


REPORTING_GROUPS = [
    ('INVENTORY_PURCHASE', 'Inventory purchase'),
    ('PAYROLL', 'Payroll'),
    ('RENT', 'Rent'),
    ('UTILITIES', 'Utilities'),
    ('OPERATING', 'Operating expense'),
    ('WASTE_SPOILAGE', 'Waste and spoilage'),
    ('FINANCE_FEES', 'Finance fees'),
    ('DEPRECIATION', 'Depreciation'),
    ('TAXES', 'Taxes'),
    ('CAPITAL_EXPENDITURE', 'Capital expenditure'),
    ('OWNER_DRAW', 'Owner withdrawal'),
    ('NON_BUSINESS', 'Non-business movement'),
    ('OTHER_INCOME', 'Other income'),
    ('REVIEW', 'Needs review'),
]


class Migration(migrations.Migration):
    dependencies = [('cashbox', '0002_expense_register_command')]

    operations = [
        migrations.AddField(
            model_name='cashboxexpensecategory',
            name='reporting_group',
            field=models.CharField(
                choices=REPORTING_GROUPS,
                db_index=True,
                default='REVIEW',
                max_length=32,
            ),
        ),
    ]
