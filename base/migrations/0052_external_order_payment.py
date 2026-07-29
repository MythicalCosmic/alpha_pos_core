import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0051_courier_role_and_payment_repair_audit'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalOrderPayment',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'uuid',
                    models.UUIDField(
                        db_index=True, default=uuid.uuid4, editable=False,
                        unique=True,
                    ),
                ),
                (
                    'synced_at',
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ('sync_version', models.PositiveIntegerField(default=1)),
                (
                    'is_deleted',
                    models.BooleanField(db_index=True, default=False),
                ),
                (
                    'branch_id',
                    models.CharField(
                        blank=True, db_index=True, default='', max_length=50,
                    ),
                ),
                (
                    'source',
                    models.CharField(
                        choices=[
                            ('COURIER', 'Courier/provider collection'),
                        ],
                        max_length=16,
                    ),
                ),
                ('source_id', models.CharField(max_length=160)),
                (
                    'method',
                    models.CharField(
                        choices=[
                            ('CASH', 'Cash'),
                            ('UZCARD', 'Uzcard'),
                            ('HUMO', 'Humo'),
                            ('CARD', 'Card'),
                            ('PAYME', 'Payme'),
                            ('MIXED', 'Mixed'),
                        ],
                        max_length=10,
                    ),
                ),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('occurred_at', models.DateTimeField(db_index=True)),
                (
                    'order',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='external_payments', to='base.order',
                    ),
                ),
            ],
            options={
                'db_table': 'external_order_payment',
                'indexes': [
                    models.Index(
                        fields=['order', 'method'],
                        name='extpay_order_method_idx',
                    ),
                    models.Index(
                        fields=['branch_id', 'occurred_at'],
                        name='extpay_branch_time_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('branch_id', 'source', 'source_id'),
                        name='uniq_external_payment_source_event',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('amount__gt', 0)),
                        name='external_payment_amount_positive',
                    ),
                    models.CheckConstraint(
                        condition=models.Q(('source', 'COURIER')),
                        name='external_payment_source_known',
                    ),
                    models.CheckConstraint(
                        condition=models.Q((
                            'method__in',
                            ['CASH', 'UZCARD', 'HUMO', 'CARD', 'PAYME'],
                        )),
                        name='external_payment_method_concrete',
                    ),
                    models.CheckConstraint(
                        condition=~models.Q(('source_id', '')),
                        name='external_payment_source_id_required',
                    ),
                ],
            },
        ),
    ]
