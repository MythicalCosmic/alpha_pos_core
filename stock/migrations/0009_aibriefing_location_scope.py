from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0008_anomaly_i18n'),
    ]

    operations = [
        migrations.AddField(
            model_name='aibriefing',
            name='location_id',
            field=models.PositiveBigIntegerField(db_index=True, default=0),
        ),
        migrations.AlterUniqueTogether(
            name='aibriefing',
            unique_together={('user_id', 'business_date', 'location_id')},
        ),
    ]
