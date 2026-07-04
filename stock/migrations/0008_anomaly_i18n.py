from django.db import migrations, models


class Migration(migrations.Migration):
    # Trilingual (uz/ru/en) copies for Anomaly Watch text. The existing
    # message/ai_explanation TextFields remain as the English fallback.

    dependencies = [
        ('stock', '0007_aibriefing_anomaly'),
    ]

    operations = [
        migrations.AddField(
            model_name='anomaly',
            name='message_i18n',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='anomaly',
            name='explanation_i18n',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
