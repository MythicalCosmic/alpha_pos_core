from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0033_appsettings_business_day_start'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='order_number',
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
