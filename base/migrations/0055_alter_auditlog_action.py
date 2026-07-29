from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0054_shift_device_ownership'),
    ]

    operations = [
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('INKASSA_PERFORM', 'Inkassa performed'),
                    ('USER_CREATE', 'User created'),
                    ('USER_UPDATE', 'User updated'),
                    ('USER_DELETE', 'User deleted'),
                    ('SHIFT_RECONCILE', 'Shift reconciled'),
                    ('ORDER_CANCEL', 'Order canceled'),
                    ('PRODUCT_PRICE_CHANGE', 'Product price changed'),
                    ('DISCOUNT_CREATE', 'Discount created'),
                    ('DISCOUNT_UPDATE', 'Discount updated'),
                    ('DISCOUNT_DELETE', 'Discount deleted'),
                    ('LOYALTY_REDEEM', 'Loyalty stamps redeemed'),
                    ('TREASURY_TRANSFER', 'Treasury transfer'),
                    ('TREASURY_EXPENSE', 'Treasury expense'),
                    ('ORDER_PAYMENT_REPAIR', 'Order payment repaired'),
                    ('FINANCIAL_REPAIR', 'Historical financial repair'),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
