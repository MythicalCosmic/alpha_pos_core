from django.db import migrations, models


# One-time refresh of the four order.* templates to the new formal/detailed
# layout (order.ready now includes the item list + reply-threads under order.new).
# Pulls the text from the seed command so there is a single source of truth.
ORDER_TYPES = {'order.new', 'order.ready', 'order.cancelled', 'order.paid'}


def refresh_order_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model('notifications', 'NotificationTemplate')
    try:
        from notifications.management.commands.seed_templates import TEMPLATES
    except Exception:
        return
    for tpl in TEMPLATES:
        if tpl.get('notification_type') in ORDER_TYPES:
            NotificationTemplate.objects.filter(
                notification_type=tpl['notification_type']
            ).update(
                name=tpl['name'],
                template_text=tpl['template_text'],
                description=tpl.get('description', ''),
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0008_notificationsettings_chat_routing'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderNotificationDispatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order_id', models.IntegerField(db_index=True, unique=True)),
                ('new_sent', models.BooleanField(default=False)),
                ('ready_sent', models.BooleanField(default=False)),
                ('paid_sent', models.BooleanField(default=False)),
                ('cancelled_sent', models.BooleanField(default=False)),
                ('new_message_ids', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.RunPython(refresh_order_templates, noop_reverse),
    ]
