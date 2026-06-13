from notifications.services.sender_service import SenderService
from notifications.helpers import format_datetime, format_money, format_prep_time

ORDER_TYPE_LABELS = {
    'HALL': 'Zalda',
    'DELIVERY': 'Yetkazib berish',
    'PICKUP': 'Olib ketish',
}


class OrderNotification:

    @classmethod
    def on_new_order(cls, order):
        items_lines = []
        for item in order.items.select_related('product').all():
            items_lines.append(
                f"  {item.product.name} x{item.quantity} — {format_money(item.price * item.quantity)} so'm"
            )

        cashier_name = '—'
        if order.cashier:
            cashier_name = f'{order.cashier.first_name} {order.cashier.last_name}'

        _, time_str = format_datetime()

        SenderService.send('order.new', {
            'display_id': order.display_id,
            'cashier_name': cashier_name,
            'order_type': ORDER_TYPE_LABELS.get(order.order_type, order.order_type),
            'total_amount': format_money(order.total_amount),
            'items_list': '\n'.join(items_lines),
            'time': time_str,
        })

    @classmethod
    def on_order_ready(cls, order_id):
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return

        prep_time = '—'
        if order.ready_at and order.created_at:
            seconds = (order.ready_at - order.created_at).total_seconds()
            prep_time = format_prep_time(seconds)

        _, time_str = format_datetime()

        SenderService.send('order.ready', {
            'display_id': order.display_id,
            'prep_time': prep_time,
            'total_amount': format_money(order.total_amount),
            'time': time_str,
        })

    @classmethod
    def on_order_cancelled(cls, order_id):
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return

        _, time_str = format_datetime()

        SenderService.send('order.cancelled', {
            'display_id': order.display_id,
            'total_amount': format_money(order.total_amount),
            'time': time_str,
        })

    @classmethod
    def on_order_paid(cls, order_id):
        from base.models import Order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return

        _, time_str = format_datetime()

        SenderService.send('order.paid', {
            'display_id': order.display_id,
            'total_amount': format_money(order.total_amount),
            'time': time_str,
        })
