from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0050_order_origin'),
    ]

    operations = [
        migrations.AddField(
            model_name='idempotencykey',
            name='request_fingerprint',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[
                    ('USER', 'User'),
                    ('ADMIN', 'Admin'),
                    ('CASHIER', 'Cashier'),
                    ('MANAGER', 'Manager'),
                    ('WAITER', 'Waiter'),
                    ('COURIER', 'Courier'),
                    ('CHEF', 'Chef'),
                ],
                default='USER',
                max_length=10,
            ),
        ),
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
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name='rolepermission',
            name='role',
            field=models.CharField(
                choices=[
                    ('USER', 'User'),
                    ('ADMIN', 'Admin'),
                    ('CASHIER', 'Cashier'),
                    ('MANAGER', 'Manager'),
                    ('WAITER', 'Waiter'),
                    ('COURIER', 'Courier'),
                    ('CHEF', 'Chef'),
                ],
                max_length=20,
                unique=True,
            ),
        ),
    ]
