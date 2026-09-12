"""Read-only, bounded evidence for loyalty and waiter rollouts."""
import json

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.db.models.functions import Coalesce

from base.models import AppSettings, Order, Table
from stock.models import ProductStockLink, StockSettings, StockTransaction


class Command(BaseCommand):
    help = 'Report loyalty and waiter inconsistencies as JSON; never changes data.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100, help='Maximum example IDs per finding.')

    def handle(self, *args, **options):
        limit = max(1, min(options['limit'], 1000))
        findings = {}

        def evidence(name, queryset):
            findings[name] = {'count': queryset.count(), 'example_ids': list(
                queryset.order_by('pk').values_list('pk', flat=True)[:limit])}

        live = Order.objects.filter(is_deleted=False, is_paid=False,
                                    status__in=['OPEN', 'PREPARING', 'READY'])
        tables = Table.objects.filter(is_deleted=False).annotate(
            has_live_ticket=Exists(live.filter(table_id=OuterRef('pk'))))
        evidence('available_table_with_live_ticket', tables.filter(status='AVAILABLE', has_live_ticket=True))
        evidence('occupied_table_without_live_ticket', tables.filter(status='OCCUPIED', has_live_ticket=False))
        duplicate_table_ids = live.exclude(table_id=None).values('table_id').annotate(
            tickets=Count('pk')).filter(tickets__gt=1).values('table_id')
        evidence('multiple_live_tickets_per_table', tables.filter(pk__in=duplicate_table_ids))
        orders = Order.objects.filter(is_deleted=False)
        evidence('legacy_waiter_identity_requires_backfill_review', orders.filter(
            waiter_id=None, user__role='WAITER'))
        evidence('waiter_shift_actor_mismatch', orders.filter(waiter_shift__isnull=False).exclude(
            waiter_id=F('waiter_shift__user_id')))
        evidence('waiter_shift_branch_mismatch', orders.filter(waiter_shift__isnull=False).exclude(
            branch_id=F('waiter_shift__branch_id')))
        policy = AppSettings.objects.filter(pk=1).first()
        stock = StockSettings.objects.filter(pk=1).first()
        if stock and stock.stock_enabled and stock.auto_deduct_on_sale:
            states = {'CREATED': ['OPEN', 'PREPARING', 'READY', 'COMPLETED'],
                      'PREPARING': ['PREPARING', 'READY', 'COMPLETED'],
                      'READY': ['READY', 'COMPLETED'], 'COMPLETED': ['COMPLETED']}
            linked = ProductStockLink.objects.filter(is_deleted=False, is_active=True).values('product_id')
            candidates = orders.filter(status__in=states.get(stock.deduct_on_order_status, [])).filter(
                items__is_deleted=False, items__product_id__in=linked).distinct()
            movement = StockTransaction.objects.filter(is_deleted=False, order_id=OuterRef('pk'), movement_type='SALE_OUT')
            evidence('stock_deduction_requires_review', candidates.annotate(has_deduction=Exists(movement)).filter(has_deduction=False))
        if apps.is_installed('smartfood'):
            from smartfood.models import Customer, LoyaltyTransaction
            from notifications.models import OrderLoyaltyCredit
            balances = Customer.objects.annotate(ledger_total=Coalesce(Sum('loyalty_txns__points'), 0))
            evidence('loyalty_balance_mismatch', balances.exclude(loyalty_points=F('ledger_total')))
            evidence('telegram_sale_credited_to_legacy_program', OrderLoyaltyCredit.objects.filter(order_id__in=Order.objects.filter(order_origin='TELEGRAM').values('pk')))
            scanned = LoyaltyTransaction.objects.filter(kind='EARN_SCAN').values('pos_order_id')
            evidence('receipt_credited_to_both_programs', OrderLoyaltyCredit.objects.filter(order_id__in=scanned))
        self.stdout.write(json.dumps({
            'read_only': True, 'requires_human_review_before_correction': True,
            'waiter_policy': {'enabled': policy.waiter_enabled,
                              'payment_mode': policy.waiter_payment_mode,
                              'require_shift': policy.waiter_require_shift} if policy else None,
            'findings': findings,
        }, indent=2, sort_keys=True))
