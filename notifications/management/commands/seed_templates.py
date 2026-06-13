from django.core.management.base import BaseCommand
from notifications.models import NotificationTemplate


TEMPLATES = [
    {
        'notification_type': 'order.new',
        'name': 'Yangi buyurtma',
        'template_text': (
            '<b>{brand}</b>\n'
            'Yangi buyurtma\n'
            '\n'
            'Buyurtma: #{display_id}\n'
            'Kassir: {cashier_name}\n'
            'Turi: {order_type}\n'
            "Jami: {total_amount} so'm\n"
            '\n'
            '{items_list}\n'
            '\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'order.ready',
        'name': 'Buyurtma tayyor',
        'template_text': (
            '<b>{brand}</b>\n'
            'Buyurtma tayyor\n'
            '\n'
            'Buyurtma: #{display_id}\n'
            'Tayyorlash vaqti: {prep_time}\n'
            "Jami: {total_amount} so'm\n"
            '\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'order.cancelled',
        'name': 'Buyurtma bekor qilindi',
        'template_text': (
            '<b>{brand}</b>\n'
            'Buyurtma bekor qilindi\n'
            '\n'
            'Buyurtma: #{display_id}\n'
            "Jami: {total_amount} so'm\n"
            '\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'order.paid',
        'name': "Buyurtma to'landi",
        'template_text': (
            '<b>{brand}</b>\n'
            "Buyurtma to'landi\n"
            '\n'
            'Buyurtma: #{display_id}\n'
            "Jami: {total_amount} so'm\n"
            '\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'shift.start',
        'name': 'Smena boshlandi',
        'template_text': (
            '<b>{brand}</b>\n'
            'Smena boshlandi\n'
            '\n'
            'Kassir: {cashier_name}\n'
            'Sana: {date}\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'shift.end',
        'name': 'Smena hisoboti',
        'template_text': (
            '<b>{brand}</b>\n'
            '{cashier_name} — Smena hisoboti\n'
            '\n'
            '{date_from} {time_from} — {date_to} {time_to}\n'
            'Davomiyligi: {duration}\n'
            '\n'
            'Buyurtmalar\n'
            'Jami: {total_orders}\n'
            'Bajarilgan: {completed_orders}\n'
            'Bekor qilingan: {cancelled_orders}\n'
            "O'rtacha tayyorlash: {avg_prep_time}\n"
            'Eng band soat: {peak_hour} ({peak_count} ta)\n'
            '\n'
            "To'lovlar\n"
            "To'langan: {paid_orders}\n"
            "To'lanmagan: {unpaid_orders}\n"
            '\n'
            'Buyurtma turlari\n'
            "Zalda: {hall_orders} ({hall_revenue} so'm)\n"
            "Yetkazib berish: {delivery_orders} ({delivery_revenue} so'm)\n"
            "Olib ketish: {pickup_orders} ({pickup_revenue} so'm)\n"
            '\n'
            'Top mahsulotlar\n'
            '{top_products_list}\n'
            '\n'
            'Moliyaviy natija\n'
            "Jami tushum: {total_revenue} so'm\n"
            "O'rtacha chek: {avg_order_value} so'm"
        ),
    },
    {
        'notification_type': 'shift.switch',
        'name': 'Smena almashdi',
        'template_text': (
            '<b>{brand}</b>\n'
            'Smena almashdi\n'
            '\n'
            'Chiqdi: {old_cashier}\n'
            'Kirdi: {new_cashier}\n'
            '\n'
            'Sana: {date}\n'
            'Vaqt: {time}'
        ),
    },
    {
        'notification_type': 'hr.contract_expiry',
        'name': 'Shartnoma muddati tugayapti',
        'template_text': (
            '<b>{brand}</b>\n'
            'Shartnoma muddati tugayapti\n'
            '\n'
            'Xodim: {employee_name}\n'
            'Shartnoma: {contract_number}\n'
            'Tugash sanasi: {end_date}\n'
            'Qolgan kunlar: {days_until}'
        ),
    },
    {
        'notification_type': 'hr.probation_end',
        'name': 'Sinov muddati tugayapti',
        'template_text': (
            '<b>{brand}</b>\n'
            'Sinov muddati tugayapti\n'
            '\n'
            'Xodim: {employee_name}\n'
            'Sinov muddati tugashi: {probation_end_date}\n'
            'Qolgan kunlar: {days_until}'
        ),
    },
    {
        'notification_type': 'hr.document_expiry',
        'name': 'Hujjat muddati tugayapti',
        'template_text': (
            '<b>{brand}</b>\n'
            'Hujjat muddati tugayapti\n'
            '\n'
            'Xodim: {employee_name}\n'
            'Hujjat: {document_title} ({document_type})\n'
            'Tugash sanasi: {expiry_date}\n'
            'Qolgan kunlar: {days_until}'
        ),
    },
    # Inbound Telegram bot replies. Edit via the templates API to change
    # what the bot says without redeploying.
    {
        'notification_type': 'telegram.start',
        'name': 'Bot welcome (/start)',
        'template_text': (
            '<b>{brand}</b>\n'
            'Salom, {first_name}!\n'
            '\n'
            "Buyurtma berish uchun /menu yozing.\n"
            "Buyurtma holatini ko'rish uchun /status yozing."
        ),
    },
    {
        'notification_type': 'telegram.unknown_command',
        'name': 'Bot unknown command fallback',
        'template_text': (
            "Kechirasiz, bu buyruqni tushunmadim.\n"
            "Mavjud buyruqlar:\n"
            "/start — boshlash\n"
            "/menu — ovqatlar ro'yxati\n"
            "/login — raqamni ulashish\n"
            "/order — savatcha va buyurtma\n"
            "/status — buyurtmalaringiz\n"
            "/loyalty — sodiqlik ballari"
        ),
    },
    {
        'notification_type': 'telegram.menu_root',
        'name': 'Bot menu (top-level categories)',
        'template_text': (
            '<b>{brand}</b>\n'
            "Ovqatlar ro'yxati:\n"
            '\n'
            '{categories_list}\n'
            '\n'
            "Toifani ochish uchun yuqoridagi /menu &lt;slug&gt; ni yuboring."
        ),
    },
    {
        'notification_type': 'telegram.menu_category',
        'name': 'Bot menu (single category)',
        'template_text': (
            '<b>{category_name}</b>\n'
            '\n'
            '{products_list}\n'
            '\n'
            "Asosiyga qaytish: /menu"
        ),
    },
    {
        'notification_type': 'telegram.menu_empty',
        'name': 'Bot menu empty fallback',
        'template_text': (
            "Hozircha ovqatlar ro'yxati bo'sh.\n"
            "Iltimos, keyinroq qayta urinib ko'ring."
        ),
    },
    {
        'notification_type': 'telegram.menu_not_found',
        'name': 'Bot menu unknown slug',
        'template_text': (
            "Bunday toifa topilmadi: {slug}\n"
            "Asosiy menyu: /menu"
        ),
    },
    {
        'notification_type': 'telegram.login_prompt',
        'name': 'Bot login prompt (/login)',
        'template_text': (
            "Salom, {first_name}!\n"
            "Buyurtmalaringizni ko'rish uchun raqamingizni ulashing.\n"
            "Quyidagi tugmani bosing."
        ),
    },
    {
        'notification_type': 'telegram.login_success',
        'name': 'Bot login success',
        'template_text': (
            "Rahmat, {first_name}!\n"
            "Raqamingiz saqlandi: {phone}\n"
            "\n"
            "Buyurtmalaringizni ko'rish uchun /status yozing."
        ),
    },
    {
        'notification_type': 'telegram.login_other_contact',
        'name': 'Bot login wrong contact',
        'template_text': (
            "Iltimos, faqat o'z raqamingizni ulashing."
        ),
    },
    {
        'notification_type': 'telegram.status_unauthenticated',
        'name': 'Bot status — not logged in',
        'template_text': (
            "Buyurtmalarni ko'rish uchun avval raqamingizni ulashing.\n"
            "/login"
        ),
    },
    {
        'notification_type': 'telegram.status_list',
        'name': 'Bot status — recent orders',
        'template_text': (
            "<b>Sizning buyurtmalaringiz</b>\n"
            "\n"
            "{orders_list}"
        ),
    },
    {
        'notification_type': 'telegram.status_empty',
        'name': 'Bot status — no orders',
        'template_text': (
            "{phone} raqami uchun so'nggi 30 kunda buyurtma topilmadi."
        ),
    },
    {
        'notification_type': 'telegram.loyalty_balance',
        'name': 'Bot loyalty — current balance',
        'template_text': (
            "<b>Sodiqlik dasturi</b>\n"
            "\n"
            "Joriy ballaringiz: {stamps}/{threshold}\n"
            "Mukofotgacha: {remaining}\n"
            "Tayyor mukofotlar: {available_rewards}\n"
            "Mukofot: {reward}"
        ),
    },
    {
        'notification_type': 'telegram.loyalty_unauthenticated',
        'name': 'Bot loyalty — not logged in',
        'template_text': (
            "Ballaringizni ko'rish uchun avval raqamingizni ulashing.\n"
            "/login"
        ),
    },
    {
        'notification_type': 'telegram.loyalty_disabled',
        'name': 'Bot loyalty — disabled',
        'template_text': (
            "Sodiqlik dasturi hozir o'chirilgan."
        ),
    },
    {
        'notification_type': 'telegram.order_cart',
        'name': 'Bot order — show cart',
        'template_text': (
            "<b>Sizning savatchangiz</b>\n"
            "\n"
            "{items_list}\n"
            "\n"
            "Jami: {total} so'm\n"
            "\n"
            "Buyurtma berish: /order checkout"
        ),
    },
    {
        'notification_type': 'telegram.order_empty',
        'name': 'Bot order — empty cart',
        'template_text': (
            "Savatcha bo'sh.\n"
            "Mahsulot qo'shing: /order add &lt;id&gt; [qty]"
        ),
    },
    {
        'notification_type': 'telegram.order_added',
        'name': 'Bot order — item added',
        'template_text': (
            "✓ {product_name} x{quantity} qo'shildi."
        ),
    },
    {
        'notification_type': 'telegram.order_removed',
        'name': 'Bot order — item removed',
        'template_text': (
            "✓ #{product_id} o'chirildi."
        ),
    },
    {
        'notification_type': 'telegram.order_cleared',
        'name': 'Bot order — cart cleared',
        'template_text': (
            "Savatcha tozalandi."
        ),
    },
    {
        'notification_type': 'telegram.order_checked_out',
        'name': 'Bot order — checked out',
        'template_text': (
            "<b>Buyurtma qabul qilindi!</b>\n"
            "\n"
            "Raqam: #{display_id}\n"
            "Jami: {total} so'm\n"
            "\n"
            "Holatni ko'rish: /status"
        ),
    },
    {
        'notification_type': 'telegram.order_help',
        'name': 'Bot order — help',
        'template_text': (
            "<b>Buyurtma berish</b>\n"
            "\n"
            "/order — savatchani ko'rish\n"
            "/order add &lt;id&gt; [qty] — mahsulot qo'shish\n"
            "/order remove &lt;id&gt; — o'chirish\n"
            "/order clear — savatchani tozalash\n"
            "/order checkout — buyurtma berish"
        ),
    },
    {
        'notification_type': 'telegram.order_no_phone',
        'name': 'Bot order — checkout without phone',
        'template_text': (
            "Buyurtma uchun avval raqamingizni ulashing.\n"
            "/login"
        ),
    },
    {
        'notification_type': 'telegram.order_invalid_product',
        'name': 'Bot order — invalid product',
        'template_text': (
            "Bunday mahsulot topilmadi: #{product_id}"
        ),
    },
]


class Command(BaseCommand):
    help = 'Seed default notification templates'

    def handle(self, *args, **options):
        created = 0
        existing = 0

        for tpl in TEMPLATES:
            _, was_created = NotificationTemplate.objects.get_or_create(
                notification_type=tpl['notification_type'],
                defaults={
                    'name': tpl['name'],
                    'template_text': tpl['template_text'],
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  Created: {tpl['notification_type']}"))
            else:
                existing += 1
                self.stdout.write(f"  Exists:  {tpl['notification_type']}")

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. Created: {created}, Already existed: {existing}'
        ))
