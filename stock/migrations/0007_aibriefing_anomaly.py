from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stock', '0006_aichat_aimessage'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIBriefing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.IntegerField(db_index=True)),
                ('business_date', models.DateField(db_index=True)),
                ('generated_at', models.DateTimeField(auto_now_add=True)),
                ('valid_until', models.DateTimeField(blank=True, null=True)),
                ('bullets', models.JSONField(default=list)),
                ('dismissed', models.BooleanField(default=False)),
            ],
            options={
                'db_table': 'ai_briefing',
                'ordering': ['-business_date'],
                'unique_together': {('user_id', 'business_date')},
            },
        ),
        migrations.CreateModel(
            name='Anomaly',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('detector', models.CharField(db_index=True, max_length=64)),
                ('severity', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], default='medium', max_length=8)),
                ('fired_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('target_kind', models.CharField(blank=True, default='', max_length=32)),
                ('target_id', models.CharField(blank=True, default='', max_length=64)),
                ('idempotency_key', models.CharField(max_length=64, unique=True)),
                ('message', models.TextField(blank=True, default='')),
                ('deep_link', models.CharField(blank=True, default='', max_length=255)),
                ('ai_explanation', models.TextField(blank=True, default='')),
                ('acked_by', models.IntegerField(blank=True, null=True)),
                ('acked_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'ai_anomaly',
                'ordering': ['-fired_at'],
            },
        ),
        migrations.CreateModel(
            name='AnomalySettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.IntegerField(db_index=True, unique=True)),
                ('muted_detectors', models.JSONField(default=list)),
                ('quiet_start', models.TimeField(blank=True, null=True)),
                ('quiet_end', models.TimeField(blank=True, null=True)),
                ('quiet_tz', models.CharField(blank=True, default='', max_length=64)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'ai_anomaly_settings',
            },
        ),
    ]
