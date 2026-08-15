"""Add preparation SLA fields to the default READY Telegram card.

Only the exact previous default is advanced. Any operator-customized template
is preserved; customized templates still receive the enhanced ``prep_time``
value from the handler, so the colored result remains visible.
"""

from django.db import migrations


PREVIOUS_TEMPLATE = {
    'name': 'Buyurtma tayyor',
    'description': (
        "Buyurtma tayyor bo'lganda avvalgi xabarning o'zi shu matn bilan "
        "yangilanadi; yangi xabar yuborilmaydi. O'zgaruvchilar: {display_id} "
        '{cashier_name} {order_type} {prep_time} {total_amount} {items_list} '
        '{accepted_at} {ready_at} {time} {brand}'
    ),
    'template_text': (
        '✅ <b>BUYURTMA TAYYOR</b>\n'
        '<b>#{display_id}</b> · {order_type}\n'
        '━━━━━━━━━━━━━━━━━━\n'
        '👤 <b>Kassir:</b> {cashier_name}\n'
        '🕐 <b>Qabul qilindi:</b> {accepted_at}\n'
        "✅ <b>Tayyor bo'ldi:</b> {ready_at}\n"
        '⏱ <b>Tayyorlash vaqti:</b> {prep_time}\n'
        '\n'
        '🛒 <b>BUYURTMA TARKIBI</b>\n'
        '{items_list}\n'
        '\n'
        "💰 <b>JAMI: {total_amount} so'm</b>\n"
        '\n'
        '🏪 {brand}'
    ),
}

NEW_TEMPLATE = {
    'name': 'Buyurtma tayyor',
    'description': (
        "Buyurtma tayyor bo'lganda avvalgi xabarning o'zi shu matn bilan "
        "yangilanadi; yangi xabar yuborilmaydi. O'zgaruvchilar: {display_id} "
        '{cashier_name} {order_type} {prep_time} {prep_elapsed} {prep_target} '
        '{prep_status_icon} {prep_status_label} {prep_status_level} '
        '{total_amount} {items_list} {accepted_at} {ready_at} {time} {brand}'
    ),
    'template_text': (
        '{prep_status_icon} <b>BUYURTMA TAYYOR · {prep_status_label}</b>\n'
        '<b>#{display_id}</b> · {order_type}\n'
        '━━━━━━━━━━━━━━━━━━\n'
        '👤 <b>Kassir:</b> {cashier_name}\n'
        '🕐 <b>Qabul qilindi:</b> {accepted_at}\n'
        "✅ <b>Tayyor bo'ldi:</b> {ready_at}\n"
        '⏱ <b>Tayyorlash vaqti:</b> {prep_elapsed}\n'
        "🎯 <b>Me'yor:</b> {prep_target}\n"
        '\n'
        '🛒 <b>BUYURTMA TARKIBI</b>\n'
        '{items_list}\n'
        '\n'
        "💰 <b>JAMI: {total_amount} so'm</b>\n"
        '\n'
        '🏪 {brand}'
    ),
}


def _replace_exact(apps, expected, replacement):
    NotificationTemplate = apps.get_model(
        'notifications', 'NotificationTemplate',
    )
    row = NotificationTemplate.objects.filter(
        notification_type='order.ready',
    ).first()
    if row is None:
        NotificationTemplate.objects.create(
            notification_type='order.ready',
            **replacement,
        )
        return
    if not all(getattr(row, field) == expected[field] for field in expected):
        return
    NotificationTemplate.objects.filter(pk=row.pk).update(**replacement)


def apply_template(apps, schema_editor):
    _replace_exact(apps, PREVIOUS_TEMPLATE, NEW_TEMPLATE)


def restore_template(apps, schema_editor):
    _replace_exact(apps, NEW_TEMPLATE, PREVIOUS_TEMPLATE)


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0012_order_lifecycle_message_edit'),
    ]

    operations = [
        migrations.RunPython(apply_template, restore_template),
    ]

