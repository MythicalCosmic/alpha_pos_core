import logging
from decimal import Decimal
from django.core.cache import cache
from django.db.models import Sum, Count, Avg, Q, F, ExpressionWrapper, DurationField
from django.db.models.functions import ExtractHour
from notifications.helpers import (
    uzb_now, format_datetime, format_money,
    format_duration_minutes, format_prep_time, UZB_TZ,
)
from notifications.services.sender_service import SenderService

logger = logging.getLogger(__name__)

SESSION_KEY = 'notif:shift:session'
SESSION_TTL = 86400


class ShiftSession:

    @classmethod
    def get(cls):
        return cache.get(SESSION_KEY)

    @classmethod
    def start(cls, user_id, user_name):
        session = {
            'user_id': user_id,
            'user_name': user_name,
            'login_time': uzb_now().isoformat(),
        }
        cache.set(SESSION_KEY, session, SESSION_TTL)
        return session

    @classmethod
    def clear(cls):
        old = cls.get()
        cache.delete(SESSION_KEY)
        return old

    @classmethod
    def get_info(cls):
        session = cls.get()
        if not session:
            return None

        from datetime import datetime
        start = datetime.fromisoformat(session['login_time'])
        if start.tzinfo is None:
            start = start.replace(tzinfo=UZB_TZ)

        now = uzb_now()
        duration_min = int((now - start).total_seconds() / 60)

        return {
            'user_id': session['user_id'],
            'user_name': session['user_name'],
            'login_time': session['login_time'],
            'duration': format_duration_minutes(duration_min),
            'duration_minutes': duration_min,
        }


class ShiftNotification:

    @classmethod
    def on_cashier_login(cls, user_id, user_name):
        current = ShiftSession.get()

        if current and current['user_id'] == user_id:
            return {'success': True, 'message': 'Same cashier already logged in'}

        if current:
            cls._send_shift_switch(current, user_id, user_name)
            ShiftSession.start(user_id, user_name)
            return {
                'success': True,
                'message': f'Shift switched to {user_name}',
                'previous_cashier': current['user_name'],
            }

        cls._send_shift_start(user_name)
        ShiftSession.start(user_id, user_name)
        return {'success': True, 'message': f'Shift started for {user_name}'}

    @classmethod
    def on_cashier_logout(cls, user_id):
        current = ShiftSession.get()

        if not current:
            return {'success': True, 'message': 'No active session', 'notification_sent': False}

        if current['user_id'] != user_id:
            return {'success': True, 'message': 'Different cashier active', 'notification_sent': False}

        cls._send_shift_end(current)
        ShiftSession.clear()
        return {
            'success': True,
            'message': f'Shift ended for {current["user_name"]}',
            'notification_sent': True,
        }

    @classmethod
    def get_session_info(cls):
        return ShiftSession.get_info()

    @classmethod
    def _get_shift_stats(cls, start_time, end_time=None, cashier_id=None):
        from base.models import Order, OrderItem, OrderRefund
        from datetime import datetime

        if end_time is None:
            end_time = uzb_now()
        if isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time)
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UZB_TZ)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=UZB_TZ)

        base_q = Q(created_at__gte=start_time, created_at__lte=end_time)
        if cashier_id:
            base_q &= Q(cashier_id=cashier_id)

        orders = Order.objects.filter(base_q, is_deleted=False)
        paid_orders = Order.objects.filter(
            is_deleted=False,
            is_paid=True,
            paid_at__gte=start_time,
            paid_at__lte=end_time,
        )
        if cashier_id:
            paid_orders = paid_orders.filter(cashier_id=cashier_id)
        refunds = OrderRefund.objects.filter(
            is_deleted=False,
            refunded_at__gte=start_time,
            refunded_at__lte=end_time,
        )
        if cashier_id:
            refunds = refunds.filter(cashier_id=cashier_id)

        agg = orders.aggregate(
            total=Count('id'),
            unpaid=Count('id', filter=Q(is_paid=False) & ~Q(status='CANCELED')),
            cancelled=Count('id', filter=Q(status='CANCELED')),
            completed=Count('id', filter=Q(status='COMPLETED')),
        )

        money = paid_orders.aggregate(
            revenue=Sum('total_amount'), avg_value=Avg('total_amount'),
            paid=Count('id'),
        )
        gross_revenue = money['revenue'] or Decimal('0')
        refund_total = refunds.aggregate(total=Sum('amount'))['total'] or Decimal('0')
        revenue = gross_revenue - refund_total
        avg_value = money['avg_value'] or Decimal('0')

        ready_orders = orders.filter(
            ready_at__isnull=False, status__in=['READY', 'COMPLETED']
        ).annotate(
            prep=ExpressionWrapper(F('ready_at') - F('created_at'), output_field=DurationField())
        )
        avg_prep = 0
        if ready_orders.exists():
            total_secs = sum((o.prep.total_seconds() for o in ready_orders if o.prep), 0)
            avg_prep = total_secs / ready_orders.count()

        type_data = orders.values('order_type').annotate(count=Count('id'))
        type_revenue = {
            row['order_type']: row['rev'] or Decimal('0')
            for row in paid_orders.values('order_type').annotate(
                rev=Sum('total_amount'),
            )
        }
        for row in refunds.values('order__order_type').annotate(
            rev=Sum('amount'),
        ):
            order_type = row['order__order_type']
            type_revenue[order_type] = (
                type_revenue.get(order_type, Decimal('0'))
                - (row['rev'] or Decimal('0'))
            )
        order_types = {
            'HALL': {'count': 0, 'revenue': Decimal('0')},
            'DELIVERY': {'count': 0, 'revenue': Decimal('0')},
            'PICKUP': {'count': 0, 'revenue': Decimal('0')},
        }
        for t in type_data:
            if t['order_type'] in order_types:
                order_types[t['order_type']]['count'] = t['count']
        for order_type, type_rev in type_revenue.items():
            if order_type in order_types:
                order_types[order_type]['revenue'] = type_rev

        hourly = orders.annotate(
            hour=ExtractHour('created_at')
        ).values('hour').annotate(count=Count('id')).order_by('hour')

        peak = {'hour': 0, 'count': 0}
        for h in hourly:
            if h['count'] > peak['count']:
                peak = {'hour': h['hour'], 'count': h['count']}

        item_q = Q(
            order__paid_at__gte=start_time,
            order__paid_at__lte=end_time,
            order__is_paid=True,
        )
        if cashier_id:
            item_q &= Q(order__cashier_id=cashier_id)

        sale_items = OrderItem.objects.filter(
            item_q, is_deleted=False, order__is_deleted=False,
        )
        from base.services.refund_lines import refund_item_events
        refund_filters = {
            'refunded_at__gte': start_time,
            'refunded_at__lte': end_time,
        }
        if cashier_id:
            refund_filters['cashier_id'] = cashier_id
        refund_items = refund_item_events(**refund_filters)
        from base.services.revenue import net_grouped_items
        top = net_grouped_items(
            sale_items, refund_items, ('product__name',),
        )
        top.sort(key=lambda row: (-(row['q'] or 0), row['product__name'] or ''))
        top = [
            {
                'product__name': row['product__name'],
                'qty': row['q'],
                'rev': row['rev'],
            }
            for row in top[:5]
        ]

        duration_min = int((end_time - start_time).total_seconds() / 60)

        return {
            'total_orders': agg['total'],
            'paid_orders': money['paid'],
            'unpaid_orders': agg['unpaid'],
            'cancelled_orders': agg['cancelled'],
            'completed_orders': agg['completed'],
            'total_revenue': revenue,
            'gross_revenue': gross_revenue,
            'refunds': refund_total,
            'refunded_orders': refunds.count(),
            'avg_order_value': avg_value,
            'avg_prep_seconds': avg_prep,
            'order_types': order_types,
            'peak_hour': peak,
            'top_products': top,
            'duration_minutes': duration_min,
        }

    @classmethod
    def _format_top_products(cls, products):
        if not products:
            return "Ma'lumot yo'q"
        lines = []
        for i, p in enumerate(products, 1):
            name = p['product__name']
            qty = p['qty']
            rev = format_money(p['rev'] or 0)
            lines.append(f"{i}. {name} — {qty} ta ({rev} so'm)")
        return '\n'.join(lines)

    @classmethod
    def _send_shift_start(cls, user_name):
        date_str, time_str = format_datetime()
        SenderService.send('shift.start', {
            'cashier_name': user_name,
            'date': date_str,
            'time': time_str,
        })

    @classmethod
    def _send_shift_end(cls, session):
        from datetime import datetime

        now = uzb_now()
        date_str, time_str = format_datetime(now)
        start = datetime.fromisoformat(session['login_time'])
        if start.tzinfo is None:
            start = start.replace(tzinfo=UZB_TZ)
        start_date, start_time = format_datetime(start)

        stats = cls._get_shift_stats(start, now, session['user_id'])

        SenderService.send('shift.end', {
            'cashier_name': session['user_name'],
            'date_from': start_date,
            'time_from': start_time,
            'date_to': date_str,
            'time_to': time_str,
            'duration': format_duration_minutes(stats['duration_minutes']),
            'total_orders': stats['total_orders'],
            'completed_orders': stats['completed_orders'],
            'cancelled_orders': stats['cancelled_orders'],
            'avg_prep_time': format_prep_time(stats['avg_prep_seconds']),
            'peak_hour': f"{stats['peak_hour']['hour']:02d}:00",
            'peak_count': stats['peak_hour']['count'],
            'paid_orders': stats['paid_orders'],
            'unpaid_orders': stats['unpaid_orders'],
            'hall_orders': stats['order_types']['HALL']['count'],
            'hall_revenue': format_money(stats['order_types']['HALL']['revenue']),
            'delivery_orders': stats['order_types']['DELIVERY']['count'],
            'delivery_revenue': format_money(stats['order_types']['DELIVERY']['revenue']),
            'pickup_orders': stats['order_types']['PICKUP']['count'],
            'pickup_revenue': format_money(stats['order_types']['PICKUP']['revenue']),
            'top_products_list': cls._format_top_products(stats['top_products']),
            'total_revenue': format_money(stats['total_revenue']),
            'avg_order_value': format_money(stats['avg_order_value']),
        })

    @classmethod
    def _send_shift_switch(cls, old_session, new_user_id, new_user_name):
        cls._send_shift_end(old_session)

        date_str, time_str = format_datetime()
        SenderService.send('shift.switch', {
            'old_cashier': old_session['user_name'],
            'new_cashier': new_user_name,
            'date': date_str,
            'time': time_str,
        })
