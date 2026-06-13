from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0006_alter_order_is_paid_alter_order_status_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='SyncQueueRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('model_name', models.CharField(db_index=True, max_length=100)),
                ('record_uuid', models.UUIDField(db_index=True)),
                ('payload', models.JSONField()),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'sync_queue_record',
                'ordering': ['created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='syncqueuerecord',
            constraint=models.UniqueConstraint(fields=('model_name', 'record_uuid'), name='uniq_sync_queue_model_uuid'),
        ),
    ]
