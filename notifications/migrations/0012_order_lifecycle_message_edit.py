"""Add one-message order lifecycle state and refresh untouched defaults.

The template transition is deliberately conditional:

* a missing template is created with the new default;
* an exact previous default advances to the new default;
* any operator-customized row is preserved byte-for-byte.

Binary rollback must run this migration backwards (to ``notifications 0011``)
*before* starting old code, because the new default template uses context fields
introduced by the matching handler release.  Reverse migration restores an exact
new default, but preserves a template edited by an operator after deployment.
"""

from django.db import migrations, models


NEW_TEMPLATES = {
    'order.new': {
        'name': 'Yangi buyurtma',
        'description': (
            "Yangi buyurtma kelganda bir marta yuboriladi; tayyor bo'lganda shu "
            "xabarning o'zi yangilanadi. O'zgaruvchilar: {display_id} "
            '{cashier_name} {order_type} {total_amount} {items_list} '
            '{accepted_at} {time} {brand}'
        ),
        'template_text': (
            '🟠 <b>YANGI BUYURTMA · QABUL QILINDI</b>\n'
            '<b>#{display_id}</b> · {order_type}\n'
            '━━━━━━━━━━━━━━━━━━\n'
            '👤 <b>Kassir:</b> {cashier_name}\n'
            '🕐 <b>Qabul qilindi:</b> {accepted_at}\n'
            '\n'
            '🛒 <b>BUYURTMA TARKIBI</b>\n'
            '{items_list}\n'
            '\n'
            "💰 <b>JAMI: {total_amount} so'm</b>\n"
            '\n'
            '⏳ <i>Tayyorlanmoqda…</i>\n'
            '🏪 {brand}'
        ),
    },
    'order.ready': {
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
    },
}


PREVIOUS_TEMPLATES = {
    'order.new': {
        'name': 'Yangi buyurtma',
        'description': (
            "Yangi buyurtma kelganda yuboriladi. Mavjud o'zgaruvchilar: "
            '{display_id} {cashier_name} {order_type} {total_amount} '
            '{items_list} {time} {brand}'
        ),
        'template_text': (
            '🆕 <b>YANGI BUYURTMA</b>\n'
            '━━━━━━━━━━━━━━\n'
            '🧾 Buyurtma: <b>#{display_id}</b>\n'
            '👤 Kassir: {cashier_name}\n'
            '📍 Turi: {order_type}\n'
            "💰 Jami: <b>{total_amount} so'm</b>\n"
            '\n'
            '🛒 <b>Tarkibi:</b>\n'
            '{items_list}\n'
            '\n'
            '🕒 Qabul qilindi: {time}\n'
            '<i>{brand}</i>'
        ),
    },
    'order.ready': {
        'name': 'Buyurtma tayyor',
        'description': (
            "Buyurtma tayyor bo'lganda yangi buyurtma xabariga JAVOB sifatida "
            'yuboriladi. O\'zgaruvchilar: {display_id} {prep_time} '
            '{total_amount} {items_list} {time} {brand}'
        ),
        'template_text': (
            '✅ <b>BUYURTMA TAYYOR</b>\n'
            '━━━━━━━━━━━━━━\n'
            '🧾 Buyurtma: <b>#{display_id}</b>\n'
            '⏱ Tayyorlanish vaqti: <b>{prep_time}</b>\n'
            "💰 Jami: {total_amount} so'm\n"
            '\n'
            '🛒 <b>Tarkibi:</b>\n'
            '{items_list}\n'
            '\n'
            "🕒 Tayyor bo'ldi: {time}\n"
            '<i>{brand}</i>'
        ),
    },
}


def _transition_templates(
    apps,
    *,
    expected,
    replacement,
    create_missing=False,
):
    NotificationTemplate = apps.get_model(
        'notifications',
        'NotificationTemplate',
    )
    fields = ('name', 'description', 'template_text')
    for notification_type, replacement_values in replacement.items():
        row = NotificationTemplate.objects.filter(
            notification_type=notification_type,
        ).first()
        if row is None:
            if create_missing:
                NotificationTemplate.objects.create(
                    notification_type=notification_type,
                    **replacement_values,
                )
            continue

        expected_values = expected.get(notification_type) or {}
        if not all(
            getattr(row, field) == expected_values.get(field)
            for field in fields
        ):
            # The operator customized at least one visible field.  A deployment
            # or rollback must never erase that work.
            continue

        NotificationTemplate.objects.filter(pk=row.pk).update(
            **replacement_values,
        )


def apply_new_templates(apps, schema_editor):
    _transition_templates(
        apps,
        expected=PREVIOUS_TEMPLATES,
        replacement=NEW_TEMPLATES,
        create_missing=True,
    )


def restore_previous_templates(apps, schema_editor):
    _transition_templates(
        apps,
        expected=NEW_TEMPLATES,
        replacement=PREVIOUS_TEMPLATES,
        create_missing=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0011_notificationlog_recipient_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='ordernotificationdispatch',
            name='new_recipient_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(
            apply_new_templates,
            restore_previous_templates,
        ),
    ]
