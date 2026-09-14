import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hr', '0012_backfill_legacy_treasury_expenses'),
    ]

    operations = [
        migrations.AddField(
            model_name='expensecategory',
            name='cost_behavior',
            field=models.CharField(
                choices=[
                    ('UNCLASSIFIED', 'Unclassified'),
                    ('FIXED', 'Fixed'),
                    ('VARIABLE', 'Variable'),
                    ('MIXED', 'Mixed'),
                    ('ONE_TIME', 'One-time'),
                ],
                db_index=True,
                default='UNCLASSIFIED',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='expensecategory',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='subcategories',
                to='hr.expensecategory',
            ),
        ),
        migrations.AddField(
            model_name='expense',
            name='category_cost_behavior_snapshot',
            field=models.CharField(
                choices=[
                    ('UNCLASSIFIED', 'Unclassified'),
                    ('FIXED', 'Fixed'),
                    ('VARIABLE', 'Variable'),
                    ('MIXED', 'Mixed'),
                    ('ONE_TIME', 'One-time'),
                ],
                db_index=True,
                default='UNCLASSIFIED',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='expense',
            name='category_parent_code_snapshot',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='expense',
            name='category_parent_name_snapshot',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='expense',
            name='category_reporting_group_snapshot',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name='expensecategory',
            constraint=models.CheckConstraint(
                condition=~models.Q(('parent', models.F('id'))),
                name='hr_expcat_parent_not_self',
            ),
        ),
        migrations.AddIndex(
            model_name='expensecategory',
            index=models.Index(
                fields=['parent', 'is_active', 'sort_order'],
                name='hr_expcat_tree_idx',
            ),
        ),
    ]
