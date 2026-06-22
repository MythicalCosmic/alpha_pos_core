from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0031_customer_alter_rolepermission_role_alter_user_role_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='payment_requested_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
