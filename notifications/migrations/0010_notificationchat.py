from django.db import migrations, models


def populate_chats(apps, schema_editor):
    """Seed NotificationChat rows from the existing chat_ids + chat_routing so
    the new admin-editable surface starts in sync with what was configured."""
    NotificationSettings = apps.get_model('notifications', 'NotificationSettings')
    NotificationChat = apps.get_model('notifications', 'NotificationChat')
    s = NotificationSettings.objects.filter(pk=1).first()
    if not s:
        return
    routing = s.chat_routing or {}
    for cid in (s.chat_ids or []):
        cid = str(cid)
        entry = routing.get(cid) or {}
        events = entry.get('events') if isinstance(entry, dict) else {}
        events = events if isinstance(events, dict) else {}
        NotificationChat.objects.get_or_create(chat_id=cid, defaults={
            'label': (entry.get('label', '') if isinstance(entry, dict) else ''),
            'is_enabled': True,
            'recv_orders': bool(events.get('order_paid', True)),
            'recv_shifts': bool(events.get('daily', True)),
            'recv_contracts': bool(events.get('contract', True)),
            'recv_documents': bool(events.get('document', True)),
            'recv_system': bool(events.get('system', True)),
        })


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0009_ordernotificationdispatch_formal_order_templates'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationChat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('chat_id', models.CharField(db_index=True, max_length=64, unique=True)),
                ('label', models.CharField(blank=True, default='', help_text='e.g. "Owner", "Kitchen group"', max_length=100)),
                ('is_enabled', models.BooleanField(default=True)),
                ('recv_orders', models.BooleanField(default=True, verbose_name='Buyurtmalar / Orders')),
                ('recv_shifts', models.BooleanField(default=True, verbose_name='Smena & kunlik / Shift & daily')),
                ('recv_contracts', models.BooleanField(default=True, verbose_name='Shartnomalar / Contracts')),
                ('recv_documents', models.BooleanField(default=True, verbose_name='Hujjatlar / Documents')),
                ('recv_system', models.BooleanField(default=True, verbose_name='Tizim / System')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'notification chat',
                'ordering': ['chat_id'],
            },
        ),
        migrations.RunPython(populate_chats, noop_reverse),
    ]
