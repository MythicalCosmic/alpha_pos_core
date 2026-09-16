import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hr', '0013_expense_category_hierarchy'),
        ('stock', '0020_supplier_opening_balances'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExpenseSupplierLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch_id', models.CharField(db_index=True, max_length=50)),
                ('note', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('expense', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='supplier_link', to='hr.expense')),
                ('linked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expense_supplier_links', to='base.user')),
                ('supplier', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='expense_links', to='stock.supplier')),
            ],
            options={
                'indexes': [models.Index(fields=['branch_id', 'supplier'], name='hr_expenses_branch__1141fa_idx')],
            },
        ),
    ]
