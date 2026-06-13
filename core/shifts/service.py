import logging
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.db.models import Q, Sum, Count, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from base.repositories.shift import ShiftTemplateRepository, ShiftRepository, CashReconciliationRepository
from base.helpers.response import ServiceResponse
from base.models import Order, Shift

logger = logging.getLogger(__name__)


class ShiftTemplateService:
    @staticmethod
    def list():
        templates = ShiftTemplateRepository.get_active()
        data = [
            {
                'id': t.id,
                'uuid': str(t.uuid),
                'name': t.name,
                'start_time': t.start_time.strftime('%H:%M') if t.start_time else None,
                'end_time': t.end_time.strftime('%H:%M') if t.end_time else None,
                'is_active': t.is_active,
            }
            for t in templates
        ]
        return ServiceResponse.success(data=data)

    @staticmethod
    def get(template_id):
        template = ShiftTemplateRepository.get_by_id(template_id)
        if not template:
            return ServiceResponse.not_found("Shift template not found")
        return ServiceResponse.success(data={
            'id': template.id,
            'uuid': str(template.uuid),
            'name': template.name,
            'start_time': template.start_time.strftime('%H:%M') if template.start_time else None,
            'end_time': template.end_time.strftime('%H:%M') if template.end_time else None,
            'is_active': template.is_active,
        })

    @staticmethod
    def create(name, start_time, end_time):
        if not name or not start_time or not end_time:
            return ServiceResponse.error("Name, start_time and end_time are required")
        template = ShiftTemplateRepository.create(
            name=name,
            start_time=start_time,
            end_time=end_time,
        )
        return ServiceResponse.created(data={
            'id': template.id,
            'uuid': str(template.uuid),
            'name': template.name,
            'start_time': template.start_time.strftime('%H:%M') if template.start_time else None,
            'end_time': template.end_time.strftime('%H:%M') if template.end_time else None,
            'is_active': template.is_active,
        })

    @staticmethod
    def update(template_id, **kwargs):
        template = ShiftTemplateRepository.get_by_id(template_id)
        if not template:
            return ServiceResponse.not_found("Shift template not found")
        allowed = {'name', 'start_time', 'end_time', 'is_active'}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        template = ShiftTemplateRepository.update(template, **updates)
        return ServiceResponse.success(data={
            'id': template.id,
            'uuid': str(template.uuid),
            'name': template.name,
            'start_time': template.start_time.strftime('%H:%M') if template.start_time else None,
            'end_time': template.end_time.strftime('%H:%M') if template.end_time else None,
            'is_active': template.is_active,
        })

    @staticmethod
    def delete(template_id):
        template = ShiftTemplateRepository.get_by_id(template_id)
        if not template:
            return ServiceResponse.not_found("Shift template not found")
        ShiftTemplateRepository.delete(template)
        return ServiceResponse.success(message="Shift template deleted")


class ShiftService:
    @staticmethod
    def list(page=1, per_page=20, user_id=None, status=None, date_from=None, date_to=None):
        qs = ShiftRepository.get_all().select_related('user', 'shift_template')

        if user_id:
            qs = qs.filter(user_id=user_id)
        if status:
            qs = qs.filter(status=status.upper())
        if date_from:
            qs = qs.filter(start_time__gte=date_from)
        if date_to:
            qs = qs.filter(start_time__lte=date_to)

        page_obj, paginator = ShiftRepository.paginate(qs, page, per_page)
        data = [ShiftService._serialize_shift(s) for s in page_obj]
        return ServiceResponse.success(data={
            'shifts': data,
            'pagination': {
                'page': page_obj.number,
                'per_page': per_page,
                'total': paginator.count,
                'pages': paginator.num_pages,
            },
        })

    @staticmethod
    def get(shift_id, actor=None):
        shift = ShiftRepository.get_with_relations(shift_id)
        if not shift:
            return ServiceResponse.not_found("Shift not found")

        # A plain cashier may only see their own shift; managers/admins see any.
        if actor is not None and getattr(actor, 'role', None) not in ('ADMIN', 'MANAGER') \
                and shift.user_id != actor.id:
            return ServiceResponse.forbidden("You can only view your own shift")

        data = ShiftService._serialize_shift(shift, detail=True)

        reconciliation = CashReconciliationRepository.get_for_shift(shift_id)
        if reconciliation:
            data['reconciliation'] = {
                'id': reconciliation.id,
                'expected_cash': str(reconciliation.expected_cash),
                'actual_cash': str(reconciliation.actual_cash),
                'difference': str(reconciliation.difference),
                'notes': reconciliation.notes,
                'reconciled_by': {
                    'id': reconciliation.reconciled_by.id,
                    'name': f"{reconciliation.reconciled_by.first_name} {reconciliation.reconciled_by.last_name}".strip(),
                } if reconciliation.reconciled_by else None,
                'created_at': reconciliation.created_at.isoformat() if reconciliation.created_at else None,
            }

        return ServiceResponse.success(data=data)

    @staticmethod
    def start_shift(user_id, shift_template_id=None):
        active = ShiftRepository.get_active_for_user(user_id)
        if active:
            return ServiceResponse.error("User already has an active shift")

        kwargs = {
            'user_id': user_id,
            'start_time': timezone.now(),
            'status': 'ACTIVE',
        }
        if shift_template_id:
            template = ShiftTemplateRepository.get_by_id(shift_template_id)
            if not template:
                return ServiceResponse.not_found("Shift template not found")
            kwargs['shift_template'] = template

        shift = ShiftRepository.create(**kwargs)
        shift = ShiftRepository.get_with_relations(shift.id)
        return ServiceResponse.created(data=ShiftService._serialize_shift(shift))

    @staticmethod
    @transaction.atomic
    def end_shift(shift_id, user_id, notes, actor=None, counted=None):
        # Row-lock the shift first so two concurrent end_shift calls can't
        # both pass the ACTIVE guard and double-write the final stats.
        try:
            Shift.objects.select_for_update().get(pk=shift_id, is_deleted=False)
        except Shift.DoesNotExist:
            return ServiceResponse.not_found("Shift not found")
        shift = ShiftRepository.get_with_relations(shift_id)
        if not shift:
            return ServiceResponse.not_found("Shift not found")
        # Ownership: a cashier may only end their own shift; a manager/admin may
        # close anyone's (e.g. a till a cashier walked away from).
        if actor is not None and getattr(actor, 'role', None) not in ('ADMIN', 'MANAGER') \
                and shift.user_id != actor.id:
            return ServiceResponse.forbidden("You can only end your own shift")
        if shift.status != 'ACTIVE':
            return ServiceResponse.error("Shift is not active")

        # A shift can't be closed while it still has work in flight: an order the
        # cashier opened this shift that hasn't been served or cancelled. Closing
        # over an open order would freeze the drawer mid-sale and orphan the order
        # on the kitchen line. The cashier must complete or cancel them first.
        open_orders = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            created_at__gte=shift.start_time,
            status__in=[Order.Status.OPEN, Order.Status.PREPARING, Order.Status.READY],
        ).count()
        if open_orders:
            return ServiceResponse.error(
                f"Cannot close shift while {open_orders} order(s) are still open. "
                "Complete or cancel them first."
            )

        now = timezone.now()

        # total_orders = orders TAKEN this shift, attributed by created_at.
        orders_taken = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            created_at__gte=shift.start_time,
            created_at__lte=now,
        ).aggregate(total_orders=Count('id'))

        # Revenue and cash are attributed by paid_at, NOT created_at: the cash
        # actually entered THIS shift's drawer when the order was paid. Filtering
        # by created_at mis-credits an order created near the end of one shift but
        # paid in the next, so neither shift reconciles against its physical cash.
        #
        # cash_collected separates physical cash from card/Payme so the
        # reconciliation step (expected_cash vs actual_cash) doesn't report every
        # card-paying cashier as short on cash. Legacy paid orders pre-payment_method
        # use NULL: treat them as CASH so historical shifts don't suddenly read zero.
        money = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            is_paid=True,
            paid_at__gte=shift.start_time,
            paid_at__lte=now,
        ).exclude(status='CANCELED').aggregate(
            total_revenue=Coalesce(
                Sum('total_amount'),
                Decimal('0.00'),
                output_field=DecimalField(),
            ),
            cash_collected=Coalesce(
                Sum(
                    'total_amount',
                    filter=Q(payment_method='CASH') | Q(payment_method__isnull=True),
                ),
                Decimal('0.00'),
                output_field=DecimalField(),
            ),
        )

        shift = ShiftRepository.update(
            shift,
            end_time=now,
            # ENDED, not COMPLETED: the cashier has closed the shift (stats are
            # now frozen and visible) but the manager hasn't confirmed the cash
            # yet. Reconcile moves it ENDED -> COMPLETED.
            status='ENDED',
            total_orders=orders_taken['total_orders'],
            total_revenue=money['total_revenue'],
            cash_collected=money['cash_collected'],
            notes=notes or '',
        )

        # Per-type settlement rows: freeze expected (system) per tender, plus the
        # cashier's blind count + difference. The drawer figures are derived from
        # OrderPayment (cash net of cashbox expenses).
        from cashbox.services.drawer import expected_payment_totals
        from cashbox.models import ShiftPaymentTotal
        counted = counted or {}
        for method, exp in expected_payment_totals(shift).items():
            raw = counted.get(method)
            try:
                cnt = Decimal(str(raw)) if raw is not None else Decimal('0')
            except (InvalidOperation, TypeError, ValueError):
                cnt = Decimal('0')
            ShiftPaymentTotal.objects.update_or_create(
                shift=shift, method=method,
                defaults={'expected_amount': exp, 'counted_amount': cnt,
                          'difference': cnt - exp},
            )

        shift = ShiftRepository.get_with_relations(shift.id)
        return ServiceResponse.success(data=ShiftService._serialize_shift(shift))

    @staticmethod
    @transaction.atomic
    def reconcile(shift_id, actual_cash, notes, reconciled_by_id, confirmed=None):
        # Row-lock the shift first (same pattern as end_shift) so two concurrent
        # reconcile calls can't both pass the "no existing reconciliation" guard
        # and each create a CashReconciliation for the same shift.
        try:
            Shift.objects.select_for_update().get(pk=shift_id, is_deleted=False)
        except Shift.DoesNotExist:
            return ServiceResponse.not_found("Shift not found")

        shift = ShiftRepository.get_with_relations(shift_id)
        if not shift:
            return ServiceResponse.not_found("Shift not found")

        if shift.status != 'ENDED':
            return ServiceResponse.error("Shift must be ended before reconciling")

        # Re-checked AFTER acquiring the lock: the loser of a concurrent race
        # sees the winner's row here and bails instead of double-creating.
        existing = CashReconciliationRepository.get_for_shift(shift_id)
        if existing:
            return ServiceResponse.error("Reconciliation already exists for this shift")

        expected_cash = shift.cash_collected
        actual = Decimal(str(actual_cash))
        difference = actual - expected_cash

        reconciliation = CashReconciliationRepository.create(
            shift=shift,
            expected_cash=expected_cash,
            actual_cash=actual,
            difference=difference,
            notes=notes or '',
            reconciled_by_id=reconciled_by_id,
        )

        # Post the manager-confirmed money to the branch SAFE (cash) / BANK
        # (cards) and freeze the per-type confirmed figures. confirmed defaults
        # to the cashier's counted amount per method (the "copy" UX).
        from cashbox.models import ShiftPaymentTotal
        confirmed = confirmed or {}
        confirmed_cash = Decimal('0')
        confirmed_card = Decimal('0')
        for spt in ShiftPaymentTotal.objects.filter(shift=shift):
            raw = confirmed.get(spt.method)
            if raw is not None:
                try:
                    amt = Decimal(str(raw))
                except (InvalidOperation, TypeError, ValueError):
                    amt = spt.counted_amount or Decimal('0')
            else:
                amt = spt.counted_amount or Decimal('0')
            spt.confirmed_amount = amt
            spt.save(update_fields=['confirmed_amount', 'synced_at', 'sync_version'])
            if amt and amt > 0:
                if spt.method == 'CASH':
                    confirmed_cash += amt
                else:
                    confirmed_card += amt
        if confirmed_cash > 0 or confirmed_card > 0:
            from base.services.treasury_service import TreasuryService
            TreasuryService.deposit_shift(
                confirmed_cash, confirmed_card,
                performed_by=reconciliation.reconciled_by, reference_id=shift.id,
            )

        # Manager confirmed the cash: ENDED -> COMPLETED.
        ShiftRepository.update(shift, status='COMPLETED')

        return ServiceResponse.created(data={
            'id': reconciliation.id,
            'shift_id': shift.id,
            'expected_cash': str(reconciliation.expected_cash),
            'actual_cash': str(reconciliation.actual_cash),
            'difference': str(reconciliation.difference),
            'notes': reconciliation.notes,
            'reconciled_by_id': reconciled_by_id,
            'created_at': reconciliation.created_at.isoformat() if reconciliation.created_at else None,
            # Per-tender cashier-vs-manager comparison (what posted to the SAFE).
            'settlement': ShiftService._shift_settlement(shift),
        })

    @staticmethod
    def current_for_user(user_id):
        """The caller's own open shift (or None) — for the till's resume check.
        Builds the body directly so `data` is always present (null when no open
        shift), since ServiceResponse.success drops a None data key."""
        shift = ShiftRepository.get_active_for_user(user_id)
        if shift:
            shift = ShiftRepository.get_with_relations(shift.id)
        data = ShiftService._serialize_shift(shift) if shift else None
        return {"success": True, "message": "Success", "data": data}, 200

    @staticmethod
    def end_active_for_user(user_id, notes=''):
        """End the caller's own active shift. 404 if they have none open."""
        shift = ShiftRepository.get_active_for_user(user_id)
        if not shift:
            return ServiceResponse.not_found("No active shift to end")
        return ShiftService.end_shift(shift.id, user_id, notes)

    @staticmethod
    def get_active_shifts():
        shifts = ShiftRepository.filter_by_status('ACTIVE').select_related('user', 'shift_template')
        data = [ShiftService._serialize_shift(s) for s in shifts]
        return ServiceResponse.success(data=data)

    @staticmethod
    def _live_totals(shift, end):
        """Compute a shift's totals on the fly (same attribution end_shift uses).

        total_orders by created_at; revenue/cash by paid_at, cash bundling
        legacy NULL payment_method with CASH."""
        start = shift.start_time
        orders_taken = Order.objects.filter(
            is_deleted=False, cashier_id=shift.user_id,
            created_at__gte=start, created_at__lte=end,
        ).aggregate(total_orders=Count('id'))
        money = Order.objects.filter(
            is_deleted=False, cashier_id=shift.user_id, is_paid=True,
            paid_at__gte=start, paid_at__lte=end,
        ).exclude(status='CANCELED').aggregate(
            total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00'), output_field=DecimalField()),
            cash_collected=Coalesce(
                Sum('total_amount', filter=Q(payment_method='CASH') | Q(payment_method__isnull=True)),
                Decimal('0.00'), output_field=DecimalField()),
        )
        return (
            orders_taken['total_orders'] or 0,
            money['total_revenue'],
            money['cash_collected'],
        )

    @staticmethod
    def _shift_settlement(shift):
        """Per-tender cashier-vs-manager comparison (the 'expenses comparing
        cashier and manager' view): expected (system), counted (cashier's blind
        count), confirmed (manager's accepted figure that posted to the SAFE),
        and the frozen difference. Drawn from the ShiftPaymentTotal rows."""
        from cashbox.models import ShiftPaymentTotal
        rows = ShiftPaymentTotal.objects.filter(
            shift=shift, is_deleted=False).order_by('method')
        return [{
            'method': r.method,
            'expected': str(r.expected_amount),
            'counted': str(r.counted_amount),      # cashier
            'confirmed': str(r.confirmed_amount),  # manager
            'difference': str(r.difference),
        } for r in rows]

    @staticmethod
    def _shift_stats(shift, end):
        """Rich per-shift breakdowns for the shift detail page, scoped to this
        cashier and the shift window. Best-effort: degrades to safe defaults so
        a single failed aggregate never breaks the shift page."""
        from django.db.models import (
            Avg, ExpressionWrapper, DurationField, F as _F,
        )
        from django.db.models.functions import ExtractHour
        from base.models import Order, OrderItem
        start = shift.start_time
        try:
            sold = Order.objects.filter(
                is_deleted=False, cashier_id=shift.user_id,
                created_at__gte=start, created_at__lte=end,
            ).exclude(status='CANCELED')

            paid = sold.filter(is_paid=True)
            mix = paid.aggregate(
                CASH=Coalesce(Sum('total_amount', filter=Q(payment_method='CASH') | Q(payment_method__isnull=True)),
                              Decimal('0.00'), output_field=DecimalField()),
                UZCARD=Coalesce(Sum('total_amount', filter=Q(payment_method='UZCARD')),
                                Decimal('0.00'), output_field=DecimalField()),
                HUMO=Coalesce(Sum('total_amount', filter=Q(payment_method='HUMO')),
                              Decimal('0.00'), output_field=DecimalField()),
                PAYME=Coalesce(Sum('total_amount', filter=Q(payment_method='PAYME')),
                               Decimal('0.00'), output_field=DecimalField()),
                MIXED=Coalesce(Sum('total_amount', filter=Q(payment_method='MIXED')),
                               Decimal('0.00'), output_field=DecimalField()),
            )
            payment_mix = {k: str(v) for k, v in mix.items()}

            prep = sold.filter(ready_at__isnull=False).aggregate(
                avg=Avg(ExpressionWrapper(_F('ready_at') - _F('created_at'),
                                          output_field=DurationField())))
            avg_prep_seconds = prep['avg'].total_seconds() if prep['avg'] else None

            hours = list(sold.annotate(hour=ExtractHour('created_at'))
                         .values('hour').annotate(c=Count('id')).order_by('-c', 'hour'))
            peak_hour = hours[0]['hour'] if hours else None

            items = OrderItem.objects.filter(is_deleted=False, order__in=sold)
            units_sold = items.aggregate(q=Coalesce(Sum('quantity'), 0))['q']
            category_stats = list(items.values(
                'product__category_id', 'product__category__name'
            ).annotate(
                quantity=Coalesce(Sum('quantity'), 0),
                revenue=Coalesce(Sum(_F('price') * _F('quantity'), output_field=DecimalField()),
                                 Decimal('0.00')),
            ).order_by('-revenue'))
            category_stats = [{
                'category_id': c['product__category_id'],
                'category': c['product__category__name'],
                'quantity': int(c['quantity'] or 0),
                'revenue': str(c['revenue'] or 0),
            } for c in category_stats]

            return {
                'payment_mix': payment_mix,
                'units_sold': int(units_sold or 0),
                'avg_prep_seconds': avg_prep_seconds,
                'peak_hour': peak_hour,
                'category_stats': category_stats,
            }
        except Exception:
            logger.exception('shift stats computation failed (shift=%s)', shift.id)
            return {
                'payment_mix': {}, 'units_sold': 0, 'avg_prep_seconds': None,
                'peak_hour': None, 'category_stats': [],
            }

    @staticmethod
    def _serialize_shift(shift, detail=False):
        # A shift's stored totals are only written when end_shift runs, so an
        # in-progress (ACTIVE) shift would otherwise serialize as all-zero
        # "no stats". Compute them live for ACTIVE shifts (clock running to
        # now); COMPLETED/ABANDONED shifts keep their frozen end-of-shift
        # numbers. This is why stats now show before the shift is finalized.
        is_live = shift.status == 'ACTIVE' and not shift.end_time
        effective_end = shift.end_time or timezone.now()
        if is_live:
            total_orders, total_revenue, cash_collected = ShiftService._live_totals(
                shift, effective_end)
        else:
            total_orders = shift.total_orders
            total_revenue = shift.total_revenue
            cash_collected = shift.cash_collected

        duration_minutes = None
        if shift.start_time and effective_end:
            duration_minutes = int((effective_end - shift.start_time).total_seconds() / 60)

        reconciliation = None
        try:
            rec = shift.reconciliation
            if rec and not rec.is_deleted:
                reconciliation = {
                    'id': rec.id,
                    'expected_cash': str(rec.expected_cash),
                    'actual_cash': str(rec.actual_cash),
                    'difference': str(rec.difference),
                    'notes': rec.notes,
                    'reconciled_by': {
                        'id': rec.reconciled_by.id,
                        'name': f"{rec.reconciled_by.first_name} {rec.reconciled_by.last_name}".strip(),
                    } if rec.reconciled_by else None,
                    'created_at': rec.created_at.isoformat() if rec.created_at else None,
                }
        except Exception:
            logger.exception('failed to serialize shift reconciliation (shift=%s)', shift.id)

        result = {
            'id': shift.id,
            'uuid': str(shift.uuid),
            'user': {
                'id': shift.user.id,
                'uuid': str(shift.user.uuid),
                'name': f"{shift.user.first_name} {shift.user.last_name}".strip(),
            } if shift.user else None,
            'shift_template': {
                'id': shift.shift_template.id,
                'uuid': str(shift.shift_template.uuid),
                'name': shift.shift_template.name,
            } if shift.shift_template else None,
            'start_time': shift.start_time.isoformat() if shift.start_time else None,
            'end_time': shift.end_time.isoformat() if shift.end_time else None,
            'status': shift.status,
            'total_orders': total_orders,
            'total_revenue': str(total_revenue),
            'cash_collected': str(cash_collected),
            # True ⇒ figures are live (shift still running), not finalized.
            'is_live_stats': is_live,
            'duration_minutes': duration_minutes,
            'reconciliation': reconciliation,
        }
        # The shift DETAIL page gets the rich breakdowns (payment mix, products
        # sold, category stats, peak hour, avg prep) + the per-tender
        # cashier-vs-manager settlement comparison. Kept off the list serializer
        # so paging shifts doesn't run these aggregates per row.
        if detail:
            result['stats'] = ShiftService._shift_stats(shift, effective_end)
            result['settlement'] = ShiftService._shift_settlement(shift)
        return result
