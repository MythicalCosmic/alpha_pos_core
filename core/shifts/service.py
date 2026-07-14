import logging
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Count, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from base.repositories.shift import ShiftTemplateRepository, ShiftRepository, CashReconciliationRepository
from base.helpers.response import ServiceResponse
from base.models import CashReconciliation, Order, Shift, User

logger = logging.getLogger(__name__)

_MONEY_QUANTUM = Decimal('0.01')
_MAX_MONEY = Decimal('9999999999.99')


def _money(value):
    """Return a finite, 2dp Decimal accepted by signed 12,2 columns."""
    try:
        amount = Decimal(str(value)).quantize(_MONEY_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or abs(amount) > _MAX_MONEY:
        return None
    return amount


def _nonnegative_money(value):
    amount = _money(value)
    return amount if amount is not None and amount >= 0 else None


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
    def list(page=1, per_page=20, user_id=None, status=None, date_from=None,
             date_to=None, live_only=False):
        # Join the optional one-to-one and its actor in the page query. Django
        # caches both presence and absence from this outer join, so normal
        # unreconciled shifts neither query per-row nor emit exception logs.
        qs = (ShiftRepository.get_all()
              .select_related(
                  'user', 'shift_template',
                  'reconciliation', 'reconciliation__reconciled_by',
              ))

        if user_id:
            qs = qs.filter(user_id=user_id)
        if status:
            qs = qs.filter(status=status.upper())
        if date_from:
            qs = qs.filter(start_time__gte=date_from)
        if date_to:
            qs = qs.filter(start_time__lte=date_to)
        if live_only:
            # A genuinely running shift: ACTIVE with no end_time (matches is_live_stats).
            qs = qs.filter(status='ACTIVE', end_time__isnull=True)

        page_obj, paginator = ShiftRepository.paginate(qs, page, per_page)
        shifts = list(page_obj)
        # Precompute the rich list metrics for the whole page in O(1) queries,
        # then attach per row — instead of running aggregates per shift (N+1).
        # One shared `now` so a live row's total_revenue window (in _serialize_shift)
        # and its batched payment_mix window (in _batch_list_extras) are identical.
        now = timezone.now()
        extras_map = ShiftService._batch_list_extras(shifts, now=now)
        data = [ShiftService._serialize_shift(s, extras=extras_map.get(s.id), now=now)
                for s in shifts]
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
    @transaction.atomic
    def start_shift(user_id, shift_template_id=None, actor=None, branch_id=None):
        # The user row is the serialization point for concurrent starts. The
        # partial unique constraint remains the definitive cross-process guard
        # (and protects writers that bypass this service).
        user = (
            User.objects.select_for_update()
            .filter(pk=user_id, is_deleted=False)
            .first()
        )
        if user is None:
            return ServiceResponse.not_found('User not found')
        if user.status != User.UserStatus.ACTIVE:
            return ServiceResponse.error('Suspended user cannot start a shift')
        if user.role not in {
            User.RoleChoices.ADMIN,
            User.RoleChoices.MANAGER,
            User.RoleChoices.CASHIER,
            User.RoleChoices.WAITER,
        }:
            return ServiceResponse.forbidden('Only POS staff can start a shift')

        if (
            actor is not None
            and actor.id != user.id
            and getattr(actor, 'role', None) not in ('ADMIN', 'MANAGER')
        ):
            return ServiceResponse.forbidden(
                'You can only start your own shift',
            )

        user_branch = str(user.branch_id or '').strip()
        user_is_global = user_branch.lower() == 'cloud'
        mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
        requested_branch = str(branch_id or '').strip()
        if mode != 'cloud':
            node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
            if not node_branch:
                return ServiceResponse.error(
                    'This terminal has no branch identity',
                )
            if requested_branch and requested_branch != node_branch:
                return ServiceResponse.forbidden(
                    'Requested branch differs from this terminal',
                )
            operational_branch = node_branch
        else:
            operational_branch = requested_branch or (
                '' if user_is_global else user_branch
            )
        if not operational_branch:
            return ServiceResponse.error(
                'An operational branch is required to start this shift',
            )
        if not user_branch or (
            not user_is_global and user_branch != operational_branch
        ):
            return ServiceResponse.forbidden(
                'User belongs to a different branch',
            )

        active = Shift.objects.filter(
            is_deleted=False,
            user=user,
            status=Shift.Status.ACTIVE,
            end_time__isnull=True,
        ).first()
        if active:
            return ServiceResponse.error("User already has an active shift")

        kwargs = {
            'user_id': user_id,
            'start_time': timezone.now(),
            'status': 'ACTIVE',
            'branch_id': operational_branch,
        }
        if shift_template_id:
            template = ShiftTemplateRepository.get_by_id(shift_template_id)
            if not template:
                return ServiceResponse.not_found("Shift template not found")
            kwargs['shift_template'] = template

        try:
            # Inner savepoint keeps the outer transaction usable when the
            # database constraint wins a concurrent create race.
            with transaction.atomic():
                shift = ShiftRepository.create(**kwargs)
        except IntegrityError:
            return ServiceResponse.error('User already has an active shift')
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

        # Only a genuinely in-progress sale blocks the close: an OPEN cart that is
        # still UNPAID (the cashier is mid-transaction and no money has entered the
        # drawer for it). Everything else carries over and must NOT make the till
        # impossible to close:
        #   - PAID orders are settled — their cash is attributed by paid_at, so they
        #     belong to this shift's totals whether or not the kitchen has finished.
        #   - Orders already sent to the kitchen (PREPARING/READY) are committed; they
        #     stay on the line and hand over to the kitchen / next shift.
        # The old guard blocked on ANY OPEN/PREPARING/READY order regardless of
        # payment, so paid orders the kitchen never marked COMPLETED piled up and the
        # shift could never be closed at all (the bug this fixes).
        now = timezone.now()

        blocking = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=shift.start_time,
            created_at__lt=now,
            is_paid=False,
            status=Order.Status.OPEN,
        ).count()
        if blocking:
            return ServiceResponse.error(
                f"Cannot close shift while {blocking} unpaid order(s) are still open. "
                "Take payment or cancel them first."
            )

        # total_orders = orders TAKEN this shift, attributed by created_at.
        orders_taken = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=shift.start_time,
            created_at__lt=now,
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
        paid_orders = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            is_paid=True,
            paid_at__gte=shift.start_time,
            paid_at__lt=now,
        )
        # cash_collected is DERIVED from the tender split, not from
        # Sum(total_amount, filter=payment_method='CASH'): that booked a MIXED
        # order's cash leg as ZERO (the whole sale vanished from cash), and it
        # ignored the customer's change on split payments. base.services.tender is
        # the single implementation shared with the drawer and the dashboards.
        from base.services.tender import breakdown_sources_for_orders
        _split, _, _drawer_sales = breakdown_sources_for_orders(paid_orders)
        from base.models import OrderRefund
        from base.services.order_refund import refund_totals
        _refunded = refund_totals(OrderRefund.objects.filter(
            is_deleted=False, shift=shift, branch_id=shift.branch_id,
        ))
        money = {
            'total_revenue': paid_orders.aggregate(
                s=Coalesce(Sum('total_amount'), Decimal('0.00'),
                           output_field=DecimalField()))['s']
                - _refunded['amount'],
            # Gross cash taken this shift (before cashbox pay-outs; the drawer's
            # expected figure nets those separately). Refund cash reverses only
            # in the shift that returned it, never by erasing the original sale.
            'cash_collected': (
                _drawer_sales - _refunded['drawer_cash_amount']
            ),
        }

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
        #
        # CRITICAL: this is best-effort and MUST NOT be able to fail the close.
        # The shift is already persisted ENDED above; these rows are derived and
        # recomputable. We isolate the whole block in a SAVEPOINT (nested atomic)
        # so a settlement error — a missing cashbox table on a half-migrated DB, a
        # duplicate row (MultipleObjectsReturned), an unexpected tender — rolls
        # back ONLY the settlement writes, never the ENDED status. Without this,
        # any exception here propagated out of the outer @transaction.atomic and
        # reverted the close, so the till could never be closed at all (the bug).
        try:
            with transaction.atomic():
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
                                  'difference': cnt - exp,
                                  'branch_id': shift.branch_id},
                    )
        except Exception:
            logger.exception(
                'shift settlement write failed (shift=%s); closing the shift anyway',
                shift.id)

        # The shift is ENDED and persisted above. Serializing the response must
        # NOT be able to revert that: an exception in get_with_relations /
        # _serialize_shift here propagates out of the outer @transaction.atomic
        # and rolls back the ENDED write — so the till could never close on a
        # serialization hiccup. Catch it and return a minimal payload; the shift
        # is closed regardless (the caller re-reads it via /shifts/current).
        try:
            fresh = ShiftRepository.get_with_relations(shift.id)
            return ServiceResponse.success(data=ShiftService._serialize_shift(fresh))
        except Exception:
            logger.exception(
                'shift serialize after close failed (shift=%s); shift is ENDED, '
                'returning minimal payload', shift.id)
            return ServiceResponse.success(data={'id': shift.id, 'status': 'ENDED'})

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

        # Expected DRAWER cash must be NET of cash paid OUT of the drawer (cashbox
        # expenses), matching the per-tender ShiftPaymentTotal the cashier counted
        # against at close. shift.cash_collected is GROSS (Sum of CASH order totals,
        # no expense subtraction — see end_shift), so using it made the manager's
        # physical count read a FALSE shortage equal to the shift's cash paid-outs
        # (a cashier who took 6.1M cash and paid 1.58M of it out as expenses has
        # 4.52M in the drawer, not 6.1M). Prefer the frozen CASH settlement row;
        # fall back to the live net drawer figure, then to gross as a last resort.
        from cashbox.models import ShiftPaymentTotal
        settlement_rows = list(
            ShiftPaymentTotal.objects.select_for_update().filter(
                shift=shift, is_deleted=False,
            ).order_by('method')
        )
        _spt_cash = next(
            (row for row in settlement_rows if row.method == 'CASH'), None,
        )
        if _spt_cash is not None:
            expected_cash = _spt_cash.expected_amount
        else:
            try:
                from cashbox.services.drawer import expected_payment_totals
                expected_cash = expected_payment_totals(shift).get(
                    'CASH', shift.cash_collected)
            except Exception:  # noqa: BLE001 — never block reconcile on a recompute
                expected_cash = shift.cash_collected

        # Expected cash is signed shift movement, not an opening-float-aware
        # physical balance. A refund of an earlier-shift sale can legitimately
        # make it negative; the manager's actual count remains non-negative.
        expected_cash = _money(expected_cash)
        if expected_cash is None:
            return ServiceResponse.validation_error(
                errors={'expected_cash': 'Expected drawer cash is invalid'},
                message='Cannot reconcile an invalid drawer balance',
            )

        actual = _nonnegative_money(actual_cash)
        if actual is None:
            return ServiceResponse.validation_error(
                errors={'actual_cash': 'Must be a non-negative money amount'},
                message='Invalid actual cash',
            )

        # If end_shift's best-effort settlement write could not create CASH,
        # rebuild that missing identity here. A CashReconciliation must always
        # agree with a per-method CASH confirmation.
        if _spt_cash is None:
            # Keep it unsaved until every input below has passed validation;
            # returning a 422 from an @atomic function does not roll back by
            # itself, so creating it here would commit a partial reconciliation.
            _spt_cash = ShiftPaymentTotal(
                shift=shift,
                method='CASH',
                expected_amount=expected_cash,
                counted_amount=Decimal('0.00'),
                difference=-expected_cash,
                branch_id=shift.branch_id,
            )
            settlement_rows.append(_spt_cash)

        if confirmed is None:
            confirmed = {}
        if not isinstance(confirmed, dict):
            return ServiceResponse.validation_error(
                errors={'confirmed': 'Must be an object keyed by payment method'},
                message='Invalid confirmation totals',
            )

        normalized_confirmed = {}
        for key, raw in confirmed.items():
            method = str(key or '').strip().upper()
            if not method:
                return ServiceResponse.validation_error(
                    errors={'confirmed': 'Payment method cannot be blank'},
                    message='Invalid confirmation totals',
                )
            if method in normalized_confirmed and normalized_confirmed[method] != raw:
                return ServiceResponse.validation_error(
                    errors={'confirmed': f'Conflicting values supplied for {method}'},
                    message='Contradictory confirmation totals',
                )
            normalized_confirmed[method] = raw

        known_methods = {row.method for row in settlement_rows}
        unknown = sorted(set(normalized_confirmed) - known_methods)
        if unknown:
            return ServiceResponse.validation_error(
                errors={
                    'confirmed': f'Unknown payment method(s): {", ".join(unknown)}',
                },
                message='Invalid confirmation totals',
            )

        confirmation_amounts = {}
        for row in settlement_rows:
            # actual_cash is the manager's physical CASH count and therefore is
            # canonical for CASH. Other tenders keep the copy-cashier-count
            # default for backwards-compatible calls that omit `confirmed`.
            raw = (
                normalized_confirmed.get(row.method, actual)
                if row.method == 'CASH'
                else normalized_confirmed.get(row.method, row.counted_amount)
            )
            amount = _nonnegative_money(raw)
            if amount is None:
                return ServiceResponse.validation_error(
                    errors={
                        f'confirmed.{row.method}':
                            'Must be a non-negative money amount',
                    },
                    message='Invalid confirmation totals',
                )
            confirmation_amounts[row.method] = amount

        if confirmation_amounts['CASH'] != actual:
            return ServiceResponse.validation_error(
                errors={
                    'confirmed.CASH':
                        'Must equal actual_cash (the manager cash count)',
                },
                message='Contradictory cash confirmation',
            )

        difference = actual - expected_cash

        reconciliation = CashReconciliationRepository.create(
            shift=shift,
            expected_cash=expected_cash,
            actual_cash=actual,
            difference=difference,
            notes=notes or '',
            reconciled_by_id=reconciled_by_id,
            branch_id=shift.branch_id,
        )

        # Freeze the manager's per-tender confirmations. Reconciliation is an
        # audit/finalisation boundary, not a money movement: cash still lives in
        # the branch-owned CashRegister until inkassa removes it, and inkassa is
        # the sole path that credits treasury. Posting here as well booked every
        # sale twice (SHIFT_DEPOSIT followed by INKASSA).
        for spt in settlement_rows:
            spt.confirmed_amount = confirmation_amounts[spt.method]
            # The shift is the authoritative ownership boundary. Repair legacy
            # rows that inherited the cloud node's default branch while they
            # are already locked for reconciliation.
            spt.branch_id = shift.branch_id
            if spt.pk:
                spt.save(update_fields=[
                    'confirmed_amount', 'branch_id', 'synced_at',
                    'sync_version',
                ])
            else:
                spt.save()

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
            # Per-tender cashier-vs-manager audit comparison.
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
    def end_active_for_user(user_id, notes='', counted=None, actor=None):
        """End the caller's own active shift. 404 if they have none open. Threads
        the cashier's blind per-tender count (`counted`) through to end_shift so
        the ShiftPaymentTotal reconciliation rows are created on close."""
        shift = ShiftRepository.get_active_for_user(user_id)
        if not shift:
            return ServiceResponse.not_found("No active shift to end")
        return ShiftService.end_shift(shift.id, user_id, notes, actor=actor, counted=counted)

    @staticmethod
    def get_active_shifts():
        shifts = list(ShiftRepository.filter_by_status('ACTIVE')
                      .select_related(
                          'user', 'shift_template',
                          'reconciliation', 'reconciliation__reconciled_by',
                      ))
        # Same batched extras as list() so active rows carry the full field set
        # (payment_mix, items_sold, prep, peak hour, expenses, cancelled, net...).
        now = timezone.now()
        extras_map = ShiftService._batch_list_extras(shifts, now=now)
        data = [ShiftService._serialize_shift(s, extras=extras_map.get(s.id), now=now)
                for s in shifts]
        return ServiceResponse.success(data=data)

    @staticmethod
    def _live_totals(shift, end):
        """Compute a shift's totals on the fly (same attribution end_shift uses).

        total_orders by created_at; revenue/cash by paid_at, cash bundling
        legacy NULL payment_method with CASH."""
        start = shift.start_time
        orders_taken = Order.objects.filter(
            is_deleted=False, cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=start, created_at__lt=end,
        ).aggregate(total_orders=Count('id'))
        paid_orders = Order.objects.filter(
            is_deleted=False, cashier_id=shift.user_id, is_paid=True,
            branch_id=shift.branch_id,
            paid_at__gte=start, paid_at__lt=end,
        )
        money = paid_orders.aggregate(
            total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00'), output_field=DecimalField()),
        )
        # Use the same canonical tender engine as end_shift and the drawer.
        # The old live-only shortcut counted a MIXED cash+card sale as zero cash,
        # so the counter was wrong until the shift closed and totals were frozen.
        from base.services.tender import breakdown_sources_for_orders
        split, _, drawer_sales = breakdown_sources_for_orders(paid_orders)
        from base.models import OrderRefund
        from base.services.order_refund import refund_totals
        refunded = refund_totals(OrderRefund.objects.filter(
            is_deleted=False, shift=shift, branch_id=shift.branch_id,
        ))
        return (
            orders_taken['total_orders'] or 0,
            money['total_revenue'] - refunded['amount'],
            drawer_sales - refunded['drawer_cash_amount'],
        )

    @staticmethod
    def _shift_settlement(shift):
        """Per-tender cashier-vs-manager comparison (the 'expenses comparing
        cashier and manager' view): expected (system), counted (cashier's blind
        count), confirmed (manager's accepted audit figure),
        and the frozen difference. Drawn from the ShiftPaymentTotal rows."""
        from cashbox.models import ShiftPaymentTotal
        rows = ShiftPaymentTotal.objects.filter(
            shift=shift, branch_id=shift.branch_id, is_deleted=False,
        ).order_by('method')
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
        from base.models import Order, OrderItem, OrderRefund
        start = shift.start_time
        try:
            sold = Order.objects.filter(
                is_deleted=False, cashier_id=shift.user_id,
                branch_id=shift.branch_id,
                created_at__gte=start, created_at__lt=end,
            ).exclude(status='CANCELED')

            # Money belongs to the shift in which it was PAID, even when the
            # cart was created during another shift. This matches end_shift,
            # the drawer and the frozen settlement rows.
            paid = Order.objects.filter(
                is_deleted=False,
                cashier_id=shift.user_id,
                branch_id=shift.branch_id,
                is_paid=True,
                paid_at__gte=start,
                paid_at__lt=end,
            )
            # Canonical tenders. MIXED is never a bucket: a split sale is attributed
            # to its real tenders (cash is the bill portion, not the tendered cash).
            from base.services.tender import breakdown_for_orders
            _split, _detail = breakdown_for_orders(paid)
            from base.services.order_refund import refund_totals
            refunds = OrderRefund.objects.filter(
                is_deleted=False, shift=shift, branch_id=shift.branch_id,
            )
            refunded = refund_totals(refunds)
            net_split = {
                'cash': _split['cash'] - refunded['cash_amount'],
                'card': _split['card'] - refunded['card_amount'],
                'payme': _split['payme'] - refunded['payme_amount'],
                'unknown': _split['unknown'] - refunded['unknown_amount'],
            }
            payment_mix = {
                k: str(net_split[k]) for k in ('cash', 'card', 'payme')
            }
            if net_split['unknown']:
                payment_mix['unknown'] = str(net_split['unknown'])

            prep = sold.filter(ready_at__isnull=False).aggregate(
                avg=Avg(ExpressionWrapper(_F('ready_at') - _F('created_at'),
                                          output_field=DurationField())))
            avg_prep_seconds = prep['avg'].total_seconds() if prep['avg'] else None

            hours = list(sold.annotate(hour=ExtractHour('created_at'))
                         .values('hour').annotate(c=Count('id')).order_by('-c', 'hour'))
            peak_hour = hours[0]['hour'] if hours else None

            # Product/category money follows settlement events. The paid sale
            # remains at paid_at even after operational cancellation; the full
            # refund money is allocated over the original discounted lines.
            # Units reverse only for the terminal ORDER_CANCEL event; partial
            # provider refunds do not prove any menu item was returned.
            items = OrderItem.objects.filter(
                is_deleted=False,
                order__is_deleted=False,
                order__in=paid,
            )
            from base.services.refund_lines import refund_item_events
            refunded_items = refund_item_events(id__in=refunds)
            sold_units = items.aggregate(q=Coalesce(Sum('quantity'), 0))['q']
            from base.services.refund_lines import (
                REFUND_EVENT_ALIAS,
                refund_line_quantity,
                refund_line_revenue,
            )
            refunded_units = refunded_items.aggregate(
                q=Coalesce(Sum(refund_line_quantity(REFUND_EVENT_ALIAS)), 0),
            )['q']
            units_sold = (sold_units or 0) - (refunded_units or 0)
            from base.services.revenue import net_line_revenue
            def category_rows(qs, *, refund=False):
                quantity_expr = (
                    refund_line_quantity(REFUND_EVENT_ALIAS)
                    if refund else 'quantity'
                )
                revenue_expr = (
                    refund_line_revenue(REFUND_EVENT_ALIAS)
                    if refund else net_line_revenue()
                )
                return qs.values(
                    'product__category_id', 'product__category__name'
                ).annotate(
                    # Avoid shadowing F('quantity') inside net_line_revenue().
                    units=Coalesce(Sum(quantity_expr), 0),
                    revenue=Coalesce(
                        Sum(revenue_expr), Decimal('0.00'),
                    ),
                )

            category_net = {}
            for sign, rows in (
                (Decimal('1'), category_rows(items)),
                (Decimal('-1'), category_rows(refunded_items, refund=True)),
            ):
                for row in rows:
                    key = (
                        row['product__category_id'],
                        row['product__category__name'],
                    )
                    bucket = category_net.setdefault(
                        key, {'units': 0, 'revenue': Decimal('0')},
                    )
                    bucket['units'] += int(sign) * int(row['units'] or 0)
                    bucket['revenue'] += sign * Decimal(row['revenue'] or 0)
            category_stats = [{
                'category_id': key[0],
                'category': key[1],
                'quantity': values['units'],
                'revenue': str(values['revenue']),
            } for key, values in sorted(
                category_net.items(),
                key=lambda item: item[1]['revenue'],
                reverse=True,
            ) if values['units'] or values['revenue']]

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
    def _batch_list_extras(shifts, now=None):
        """Per-shift LIST metrics for a WHOLE PAGE in O(1) queries (not O(rows)).

        The manager dashboard shows these on every shift card: payment mix
        (amount + order count per tender), items sold, avg prep, peak hour, drawer
        expenses, and cancelled-order figures. Order has no shift FK, so orders
        attribute to a shift by branch_id + cashier_id + the shift's
        [start_time, end_time) window
        (end = now for a live ACTIVE shift), bucketed in Python. A FIXED set of
        grouped queries runs for the entire page regardless of row count.

        Returns {shift_id: {expenses_total, cancelled_orders_count,
        cancelled_orders_value, payment_mix, items_sold, avg_prep_seconds,
        peak_hour}}. net_revenue is added by _serialize_shift, which knows the
        row's live/stored total_revenue. Best-effort: on any failure returns the
        all-empty map so the list still renders its base fields.
        """
        from collections import defaultdict
        from base.models import OrderItem, OrderRefund
        from cashbox.models import CashboxExpense

        zero = Decimal('0.00')

        def _empty():
            return {
                'expenses_total': '0.00',
                'cancelled_orders_count': 0,
                'cancelled_orders_value': '0.00',
                'refunds_count': 0,
                'refunds_total': '0.00',
                'payment_mix': {},
                'items_sold': 0,
                'avg_prep_seconds': None,
                'peak_hour': None,
            }

        out = {s.id: _empty() for s in shifts}
        valid = [s for s in shifts if s.start_time]
        if not valid:
            return out
        try:
            now = now or timezone.now()
            # Per-(branch, cashier) window list sorted by start. Branch is part
            # of the owner key because cashier identities are global and may be
            # reused by tills at different branches.
            #
            # Each window END matches
            # _serialize_shift's effective_end EXACTLY using the SAME shared `now`,
            # so the paid_at-bucketed payment_mix reconciles to the row's
            # total_revenue by construction. Only a genuinely live shift (ACTIVE +
            # no end_time) extends to now; any OTHER null-end shift (e.g. ABANDONED)
            # gets a degenerate window so it can't scoop a later shift's orders
            # while its own serialized totals read frozen-zero.
            by_owner = defaultdict(list)
            shift_branches = {}
            for s in valid:
                if s.end_time:
                    end = s.end_time
                elif s.status == 'ACTIVE':
                    end = now
                else:
                    end = s.start_time       # non-active, no end_time -> empty window
                owner = (s.branch_id, s.user_id)
                by_owner[owner].append((s.start_time, end, s.id))
                shift_branches[s.id] = s.branch_id
            for owner in by_owner:
                by_owner[owner].sort(key=lambda t: t[0])

            def bucket(branch_id, cid, ts):
                if ts is None:
                    return None
                # Half-open end makes an exact handoff timestamp belong only to
                # the later shift. Last match still resolves malformed overlaps
                # deterministically to the latest start.
                found = None
                for start, end, sid in by_owner.get((branch_id, cid), ()):
                    if start <= ts < end:
                        found = sid
                return found

            cashier_ids = list({owner[1] for owner in by_owner})
            branch_ids = list({owner[0] for owner in by_owner})
            min_start = min(s.start_time for s in valid)
            max_end = max((s.end_time or now) for s in valid)

            # Payment mix + counts: paid sales bucketed by paid_at; immutable
            # refunds subtract from the exact shift FK that returned the money.
            # Canonical tender split per shift. ONE extra query for the payment
            # lines (never per-shift): a MIXED order contributes to BOTH cash and
            # card, so it can no longer vanish into a `MIXED` bucket.
            from base.models import OrderPayment
            from base.services.tender import (
                _courier_rows_by_order,
                split_from_rows,
            )
            mix_acc = defaultdict(
                lambda: {'cash': zero, 'card': zero, 'payme': zero, 'unknown': zero})
            paid_cnt = defaultdict(int)
            money_rows = list(Order.objects.filter(
                is_deleted=False, cashier_id__in=cashier_ids, is_paid=True,
                branch_id__in=branch_ids,
                paid_at__gte=min_start, paid_at__lt=max_end,
            ).values_list(
                'id', 'branch_id', 'cashier_id', 'paid_at',
                'total_amount', 'payment_method'))
            _ops = defaultdict(list)
            if money_rows:
                for _oid, _m, _a in OrderPayment.objects.filter(
                        is_deleted=False, order_id__in=[r[0] for r in money_rows],
                ).values_list('order_id', 'method', 'amount'):
                    _ops[_oid].append((_m, _a))
            _courier = _courier_rows_by_order([r[0] for r in money_rows])
            for oid, branch_id, cid, paid_at, amt, method in money_rows:
                sid = bucket(branch_id, cid, paid_at)
                if sid is None:
                    continue
                _s, _ = split_from_rows(
                    amt,
                    method,
                    _ops.get(oid, ()),
                    _courier.get(oid, ()),
                    order_id=oid,
                )
                acc = mix_acc[sid]
                for _k in ('cash', 'card', 'payme', 'unknown'):
                    acc[_k] += _s[_k]
                paid_cnt[sid] += 1

            refund_cnt = defaultdict(int)
            refund_total = defaultdict(lambda: Decimal('0.00'))
            for sid, row_branch, amount, cash, card, payme, unknown in OrderRefund.objects.filter(
                is_deleted=False,
                shift_id__in=list(out.keys()),
                branch_id__in=branch_ids,
            ).values_list(
                'shift_id', 'branch_id', 'amount', 'cash_amount', 'card_amount',
                'payme_amount', 'unknown_amount',
            ):
                if shift_branches.get(sid) != row_branch:
                    continue
                acc = mix_acc[sid]
                acc['cash'] -= cash or zero
                acc['card'] -= card or zero
                acc['payme'] -= payme or zero
                acc['unknown'] -= unknown or zero
                refund_cnt[sid] += 1
                refund_total[sid] += amount or zero

            # Cancelled orders (count + lost value) by created_at.
            canc_cnt = defaultdict(int)
            canc_val = defaultdict(lambda: Decimal('0.00'))
            for branch_id, cid, created_at, amt, is_paid in Order.objects.filter(
                is_deleted=False, cashier_id__in=cashier_ids, status='CANCELED',
                branch_id__in=branch_ids,
                created_at__gte=min_start, created_at__lt=max_end,
            ).values_list(
                'branch_id', 'cashier_id', 'created_at', 'total_amount', 'is_paid',
            ):
                sid = bucket(branch_id, cid, created_at)
                if sid is None:
                    continue
                canc_cnt[sid] += 1
                # Paid cancellations have an OrderRefund money event and are
                # already netted in their refunding shift. Only unpaid canceled
                # carts are potential/lost value (never realized revenue).
                if not is_paid:
                    canc_val[sid] += (amt or zero)

            # Peak hour + avg prep over the SOLD set (non-cancelled) by created_at.
            hour_cnt = defaultdict(lambda: defaultdict(int))
            prep_sum = defaultdict(float)
            prep_n = defaultdict(int)
            for branch_id, cid, created_at, ready_at in Order.objects.filter(
                is_deleted=False, cashier_id__in=cashier_ids,
                branch_id__in=branch_ids,
                created_at__gte=min_start, created_at__lt=max_end,
            ).exclude(status='CANCELED').values_list(
                'branch_id', 'cashier_id', 'created_at', 'ready_at'):
                sid = bucket(branch_id, cid, created_at)
                if sid is None:
                    continue
                # localtime() -> project-tz wall-clock hour (matches analytics).
                hour_cnt[sid][timezone.localtime(created_at).hour] += 1
                if ready_at is not None:
                    # clamp: clock skew across synced branches can make ready_at < created_at
                    prep_sum[sid] += max(0.0, (ready_at - created_at).total_seconds())
                    prep_n[sid] += 1

            # Realized units: paid sale at paid_at, full reversal at refund shift.
            units = defaultdict(int)
            for branch_id, cid, paid_at, qty in OrderItem.objects.filter(
                is_deleted=False, order__is_deleted=False,
                order__cashier_id__in=cashier_ids,
                order__branch_id__in=branch_ids,
                order__is_paid=True,
                order__paid_at__gte=min_start, order__paid_at__lt=max_end,
            ).values_list(
                'order__branch_id', 'order__cashier_id',
                'order__paid_at', 'quantity'):
                sid = bucket(branch_id, cid, paid_at)
                if sid is None:
                    continue
                units[sid] += int(qty or 0)
            from base.services.refund_lines import (
                REFUND_EVENT_ALIAS,
                refund_item_events,
                refund_line_quantity,
            )
            refund_unit_rows = (
                refund_item_events(
                    shift_id__in=list(out.keys()),
                    branch_id__in=branch_ids,
                )
                .filter(
                    order__branch_id__in=branch_ids,
                )
                .values(
                    f'{REFUND_EVENT_ALIAS}__shift_id',
                    f'{REFUND_EVENT_ALIAS}__branch_id',
                    'order__branch_id',
                )
                .annotate(q=Coalesce(Sum(
                    refund_line_quantity(REFUND_EVENT_ALIAS)
                ), 0))
            )
            for row in refund_unit_rows:
                sid = row[f'{REFUND_EVENT_ALIAS}__shift_id']
                refund_branch = row[f'{REFUND_EVENT_ALIAS}__branch_id']
                if not (
                    shift_branches.get(sid)
                    == refund_branch
                    == row['order__branch_id']
                ):
                    continue
                units[sid] -= int(row['q'] or 0)

            # Drawer expenses: CashboxExpense HAS a shift FK -> DB GROUP BY, no bucketing.
            exp_total = defaultdict(lambda: Decimal('0.00'))
            for r in (CashboxExpense.objects
                      .filter(
                          shift_id__in=list(out.keys()),
                          branch_id__in=branch_ids,
                          is_deleted=False,
                      )
                      .values('shift_id', 'branch_id')
                      .annotate(t=Coalesce(Sum('amount'), zero, output_field=DecimalField()))):
                if shift_branches.get(r['shift_id']) != r['branch_id']:
                    continue
                exp_total[r['shift_id']] = r['t'] or zero

            def money(d):
                return str((d or zero).quantize(zero))   # always 2dp, e.g. "100.00"

            for sid in out:
                # {tender: amount}. An order can contribute to two tenders, so a
                # per-tender `count` is meaningless -> `paid_orders` is emitted instead.
                _mx = mix_acc.get(sid) or {}
                pm = {k: money(v) for k, v in _mx.items() if k != 'unknown' or v}
                hc = hour_cnt.get(sid)
                if hc:
                    # busiest hour; tie -> earliest hour (matches order_by('-c','hour')).
                    hour, cnt = min(hc.items(), key=lambda kv: (-kv[1], kv[0]))
                    peak_hour = {'hour': int(hour), 'orders': int(cnt)}
                else:
                    peak_hour = None
                n = prep_n.get(sid)
                out[sid] = {
                    'expenses_total': money(exp_total.get(sid)),
                    'cancelled_orders_count': int(canc_cnt.get(sid, 0)),
                    'cancelled_orders_value': money(canc_val.get(sid)),
                    'refunds_count': int(refund_cnt.get(sid, 0)),
                    'refunds_total': money(refund_total.get(sid)),
                    'payment_mix': pm,
                    'paid_orders': int(paid_cnt.get(sid, 0)),
                    'items_sold': int(units.get(sid, 0)),
                    'avg_prep_seconds': int(round(prep_sum.get(sid, 0.0) / n)) if n else None,
                    'peak_hour': peak_hour,
                }
        except Exception:
            logger.exception('shift list extras batch failed (%s shifts)', len(shifts))
            return {s.id: _empty() for s in shifts}
        return out

    @staticmethod
    def _serialize_shift(shift, detail=False, extras=None, now=None):
        # A shift's stored totals are only written when end_shift runs, so an
        # in-progress (ACTIVE) shift would otherwise serialize as all-zero
        # "no stats". Compute them live for ACTIVE shifts (clock running to
        # now); COMPLETED/ABANDONED shifts keep their frozen end-of-shift
        # numbers. This is why stats now show before the shift is finalized.
        # `now` is threaded from list() so a live row's total_revenue window and
        # the batched payment_mix window share the SAME instant (they must match).
        now = now or timezone.now()
        is_live = shift.status == 'ACTIVE' and not shift.end_time
        effective_end = shift.end_time or now
        if is_live:
            total_orders, total_revenue, cash_collected = ShiftService._live_totals(
                shift, effective_end)
        else:
            total_orders = shift.total_orders
            total_revenue = shift.total_revenue
            cash_collected = shift.cash_collected

        def _dec(v):
            try:
                return Decimal(str(v if v is not None else 0))
            except (InvalidOperation, TypeError):
                return Decimal('0')

        def _q2(d):
            # Money as a 2dp string, backend-independent. (SQLite drops the scale
            # on Sum() so a live total comes back as '150'; Postgres keeps '150.00'.
            # Quantizing makes the API output identical on both.)
            return str(_dec(d).quantize(Decimal('0.01')))

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
        except CashReconciliation.DoesNotExist:
            # Expected state until the manager reconciles an ENDED shift.
            pass
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
            'total_revenue': _q2(total_revenue),
            'cash_collected': _q2(cash_collected),
            # True ⇒ figures are live (shift still running), not finalized.
            'is_live_stats': is_live,
            'duration_minutes': duration_minutes,
            'reconciliation': reconciliation,
        }
        # Per-shift LIST metrics (payment mix, items sold, prep, peak hour, drawer
        # expenses, cancelled orders) precomputed in ONE batched pass by
        # ShiftService.list (O(1) queries for the whole page). net_revenue is
        # derived here from the row's own live/stored total_revenue, per the FE
        # formula: realized revenue (already net of refunds) - cash expenses.
        # Canceled unpaid carts are potential sales, not realized revenue.
        if extras is not None:
            result.update(extras)
            try:
                net = (Decimal(str(total_revenue))
                       - Decimal(result['expenses_total']))
            except (InvalidOperation, TypeError):
                net = Decimal(str(total_revenue))
            result['net_revenue'] = str(net.quantize(Decimal('0.01')))

        # ── Admin Shifts page (item 11): a flat, FE-named field set on EVERY shift
        #    row (list / active / detail). Decimals are strings; reconciliation-
        #    derived fields (variance/reported/reported_by) are null until the shift
        #    is reconciled. Kept ALONGSIDE the originals for back-compat.
        _ex = extras or {}
        _tr = _dec(total_revenue)
        _cash = _dec(cash_collected)
        _exp = _dec(_ex.get('expenses_total'))
        _canc = _dec(_ex.get('cancelled_orders_value'))
        ph = _ex.get('peak_hour')
        peak_label = None
        if isinstance(ph, dict) and ph.get('hour') is not None:
            _h = int(ph['hour'])
            peak_label = f"{_h:02d}:00-{(_h + 1) % 24:02d}:00"
        rec = reconciliation  # serialized dict above, or None until reconciled
        # avg ticket over PAID orders (payment_mix counts), not total_orders —
        # total_orders includes cancelled, which would understate the ticket.
        _paid_n = int(_ex.get('paid_orders') or 0)
        result.update({
            'gross_revenue': _q2(_tr),
            'net_revenue': result.get('net_revenue') or _q2(_tr - _exp),
            # card = Uzcard+Humo+Card (Payme is its own tender, no longer folded in)
            'card_collected': _q2(_dec((_ex.get('payment_mix') or {}).get('card'))),
            'payme_collected': _q2(_dec((_ex.get('payment_mix') or {}).get('payme'))),
            'expenses_total': result.get('expenses_total', _q2(_exp)),
            'cancelled_count': int(_ex.get('cancelled_orders_count') or 0),
            'cancelled_amount': _q2(_canc),
            'expected_cash': (
                rec['expected_cash'] if rec else _q2(_cash - _exp)
            ),
            'variance': (rec['difference'] if rec else None),
            'reported': (rec['actual_cash'] if rec else None),
            'reported_by': (rec['reconciled_by'] if rec else None),
            'avg_ticket': _q2(_tr / _paid_n) if _paid_n else '0.00',
            'items_sold': int(_ex.get('items_sold') or 0),
            'avg_prep_time': _ex.get('avg_prep_seconds'),
            'peak_hour': peak_label,
        })
        # The shift DETAIL page additionally gets the HEAVY breakdowns kept off the
        # list (per-product/category stats + the per-tender cashier-vs-manager
        # settlement comparison) so paging shifts doesn't run those per row.
        if detail:
            result['stats'] = ShiftService._shift_stats(shift, effective_end)
            result['settlement'] = ShiftService._shift_settlement(shift)
        return result
