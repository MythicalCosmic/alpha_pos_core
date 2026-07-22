import hashlib
import json
import logging
from decimal import Decimal, InvalidOperation
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum, Count, DecimalField
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


def _is_global_admin(actor):
    role = str(getattr(actor, 'role', '') or '').upper()
    branch = str(getattr(actor, 'branch_id', '') or '').strip().lower()
    return role == 'ADMIN' and branch in ('', 'cloud')


def _effective_actor_branch(actor):
    branch = str(getattr(actor, 'branch_id', '') or '').strip()
    if not branch and getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
        branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    return branch


def _actor_can_access_shift(actor, shift):
    """Branch-aware shift access shared by detail/end operations."""
    if actor is None or _is_global_admin(actor):
        return True
    role = str(getattr(actor, 'role', '') or '').upper()
    actor_branch = _effective_actor_branch(actor)
    shift_branch = str(getattr(shift, 'branch_id', '') or '').strip()
    if role in ('ADMIN', 'MANAGER'):
        return bool(actor_branch) and actor_branch == shift_branch
    return (
        bool(actor_branch)
        and actor_branch == shift_branch
        and getattr(actor, 'id', None) == getattr(shift, 'user_id', None)
    )


def _scope_shift_queryset(qs, actor):
    """Apply actor ownership before pagination or global summary aggregation."""
    if actor is None or _is_global_admin(actor):
        return qs
    role = str(getattr(actor, 'role', '') or '').upper()
    actor_branch = _effective_actor_branch(actor)
    if not actor_branch:
        return qs.none()
    qs = qs.filter(branch_id=actor_branch)
    if role not in ('ADMIN', 'MANAGER'):
        qs = qs.filter(user_id=getattr(actor, 'id', None))
    return qs


def _manifest_money(value):
    amount = _money(value)
    if amount is None:
        raise ValueError('invalid settlement manifest amount')
    return str(amount)


def _manifest_time(value):
    """Stable JSON timestamp used only as immutable evidence."""
    return value.isoformat() if value is not None else None


def _compact_manifest(rows, total):
    """Commit to row identity/content without copying sale data onto Shift.

    Count and total make support inspection useful; the digest detects a row
    substitution that preserves both aggregates, which is the exact class of
    sync loss that otherwise leaves settlement money looking correct while
    product/order analytics are incomplete.
    """
    encoded = json.dumps(
        rows, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    ).encode('utf-8')
    return {
        'count': len(rows),
        'total': _manifest_money(total),
        'sha256': hashlib.sha256(encoded).hexdigest(),
    }


def _expense_manifest(shift):
    from cashbox.models import CashboxExpense

    rows = [{
        'uuid': str(expense.uuid),
        'amount': _manifest_money(expense.amount),
    } for expense in CashboxExpense.objects.filter(
        shift=shift,
        branch_id=shift.branch_id,
        is_deleted=False,
    ).order_by('uuid')]
    encoded = json.dumps(
        rows, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    ).encode('utf-8')
    total = sum((Decimal(row['amount']) for row in rows), Decimal('0.00'))
    return {
        'count': len(rows),
        'total': _manifest_money(total),
        'rows': rows,
        'sha256': hashlib.sha256(encoded).hexdigest(),
    }


def _money_evidence_manifest(shift, *, include_external=False):
    """Commit every row used by settlement and shift product analytics.

    The close header can arrive at cloud before its children. Recomputing only
    tender totals catches ordinary gaps but not a missing row replaced by a
    compensating row of the same value. These compact commitments make that
    reordering fail closed until the exact paid orders, till/external payments,
    refunds, and order items present at close have arrived. Edition-specific
    CourierPayment is not committed directly; its canonical, synced
    ExternalOrderPayment mirror is included in manifest v3. Version 2 remains
    reproducible for shifts closed during a rolling upgrade.
    """
    from base.models import (
        ExternalOrderPayment, OrderItem, OrderPayment, OrderRefund,
    )
    from cashbox.services.drawer import _shift_orders

    orders = list(
        _shift_orders(shift).select_related('cashier').order_by('uuid')
    )
    order_ids = [row.id for row in orders]
    order_rows = [{
        'uuid': str(row.uuid),
        'paid_at': _manifest_time(row.paid_at),
        'payment_method': str(row.payment_method or ''),
        'total_amount': _manifest_money(row.total_amount),
    } for row in orders]

    payments = list(
        OrderPayment.objects.filter(
            order_id__in=order_ids, is_deleted=False,
        ).select_related('order').order_by('uuid')
    )
    payment_rows = [{
        'uuid': str(row.uuid),
        'order_uuid': str(row.order.uuid),
        'method': str(row.method or ''),
        'amount': _manifest_money(row.amount),
    } for row in payments]

    external_payment_rows = []
    if include_external:
        external_payments = list(
            ExternalOrderPayment.objects.filter(
                order_id__in=order_ids, is_deleted=False,
            ).select_related('order').order_by('uuid')
        )
        external_payment_rows = [{
            'uuid': str(row.uuid),
            'order_uuid': str(row.order.uuid),
            'source': str(row.source or ''),
            'source_id': str(row.source_id or ''),
            'method': str(row.method or ''),
            'amount': _manifest_money(row.amount),
            'occurred_at': _manifest_time(row.occurred_at),
        } for row in external_payments]

    refunds = list(
        OrderRefund.objects.filter(
            shift=shift, branch_id=shift.branch_id, is_deleted=False,
        ).select_related('order').order_by('uuid')
    )
    refund_rows = []
    for row in refunds:
        card_detail = {
            str(method or '').upper(): _manifest_money(amount)
            for method, amount in sorted((row.card_detail or {}).items())
        }
        refund_rows.append({
            'uuid': str(row.uuid),
            'order_uuid': str(row.order.uuid),
            'amount': _manifest_money(row.amount),
            'cash_amount': _manifest_money(row.cash_amount),
            'drawer_cash_amount': _manifest_money(row.drawer_cash_amount),
            'card_amount': _manifest_money(row.card_amount),
            'payme_amount': _manifest_money(row.payme_amount),
            'unknown_amount': _manifest_money(row.unknown_amount),
            'card_detail': card_detail,
            'refunded_at': _manifest_time(row.refunded_at),
            'source': row.source,
            'source_id': row.source_id,
        })

    # Refund analytics reverse the original product lines, so referenced
    # orders are evidence even when their sale belonged to an earlier shift.
    item_order_ids = set(order_ids)
    item_order_ids.update(row.order_id for row in refunds)
    items = list(
        OrderItem.objects.filter(
            order_id__in=item_order_ids, is_deleted=False,
        ).select_related('order', 'product').order_by('uuid')
    )
    item_rows = [{
        'uuid': str(row.uuid),
        'order_uuid': str(row.order.uuid),
        'product_uuid': str(row.product.uuid),
        'quantity': row.quantity,
        'price': _manifest_money(row.price),
        'original_price': _manifest_money(row.original_price),
        'discount_amount': _manifest_money(row.discount_amount),
    } for row in items]
    item_total = sum(
        (Decimal(row['price']) * row['quantity'] for row in item_rows),
        Decimal('0.00'),
    )

    result = {
        'orders': _compact_manifest(
            order_rows,
            sum((Decimal(row['total_amount']) for row in order_rows),
                Decimal('0.00')),
        ),
        'order_payments': _compact_manifest(
            payment_rows,
            sum((Decimal(row['amount']) for row in payment_rows),
                Decimal('0.00')),
        ),
        'order_refunds': _compact_manifest(
            refund_rows,
            sum((Decimal(row['amount']) for row in refund_rows),
                Decimal('0.00')),
        ),
        'order_items': _compact_manifest(item_rows, item_total),
    }
    if include_external:
        result['external_order_payments'] = _compact_manifest(
            external_payment_rows,
            sum(
                (Decimal(row['amount']) for row in external_payment_rows),
                Decimal('0.00'),
            ),
        )
    return result


def _build_settlement_manifest(
    shift, settlement_rows, *, version=3, cashier_counted_methods=None,
):
    tenders = [{
        'uuid': str(row.uuid),
        'method': row.method,
        'expected': _manifest_money(row.expected_amount),
        'counted': _manifest_money(row.counted_amount),
        'difference': _manifest_money(row.difference),
    } for row in sorted(settlement_rows, key=lambda item: item.method)]
    manifest = {
        'version': version,
        'branch_id': shift.branch_id,
        'tenders': tenders,
        'expenses': _expense_manifest(shift),
        'money_evidence': _money_evidence_manifest(
            shift, include_external=version >= 3,
        ),
    }
    # A numeric zero is a valid physical count, so it cannot also mean "the
    # cashier never submitted this tender". Keep the explicit method keys in
    # the immutable close handshake. Older manifests omit the marker and are
    # interpreted conservatively by _settlement_row_status below.
    if cashier_counted_methods is not None:
        manifest['cashier_counted_methods'] = sorted({
            str(method).strip().upper()
            for method in cashier_counted_methods
            if str(method).strip()
        })
    return manifest


def _settlement_row_status(shift, row, *, reconciled):
    """Return an honest per-tender handover state.

    ENDED means the sales window was frozen; it does not prove that the cashier
    entered a physical tender count. Historically all absent counts were stored
    as zero and then every ENDED row was labelled COUNTED, making an untouched
    handover look like a full shortage. New close manifests preserve the exact
    submitted method keys. For legacy manifests, only a non-zero count proves
    that a value was supplied; zero remains safely UNCOUNTED.
    """
    if reconciled:
        return 'CONFIRMED'
    if shift.status not in ('ENDED', 'COMPLETED'):
        return 'OPEN'

    manifest = shift.settlement_manifest or {}
    if 'cashier_counted_methods' in manifest:
        submitted = {
            str(method).strip().upper()
            for method in (manifest.get('cashier_counted_methods') or [])
            if str(method).strip()
        }
        return 'COUNTED' if str(row.method).upper() in submitted else 'UNCOUNTED'

    counted = _money(row.counted_amount)
    return 'COUNTED' if counted not in (None, Decimal('0.00')) else 'UNCOUNTED'


def _shift_tender_integrity_error(shift, evidence_end):
    """Require immutable tender evidence for every post-upgrade paid sale."""
    if not shift.treasury_settlement_eligible:
        return None

    missing_paid_at = list(
        Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=shift.start_time,
            created_at__lt=evidence_end,
            is_paid=True,
            paid_at__isnull=True,
        ).values_list('id', flat=True)[:11]
    )
    if missing_paid_at:
        order_ids = ', '.join(str(order_id) for order_id in missing_paid_at[:10])
        suffix = '' if len(missing_paid_at) <= 10 else ', ...'
        return (
            'Paid orders are missing their payment timestamp '
            f'(order ids: {order_ids}{suffix}); repair or re-sync their '
            'payment headers before closing the shift'
        )

    paid_orders = Order.objects.filter(
        is_deleted=False,
        cashier_id=shift.user_id,
        branch_id=shift.branch_id,
        is_paid=True,
        paid_at__gte=shift.start_time,
        paid_at__lt=evidence_end,
    )
    from base.services.tender import tender_integrity_issues
    issues = tender_integrity_issues(paid_orders, require_concrete=True)
    if not issues:
        return None

    order_ids = ', '.join(str(issue['order_id']) for issue in issues[:10])
    suffix = '' if len(issues) <= 10 else ', ...'
    return (
        f'{len(issues)} paid order(s) lack complete tender evidence '
        f'(order ids: {order_ids}{suffix}); repair or re-sync their payments '
        'before closing the shift'
    )


def _settlement_bundle_error(shift, settlement_rows):
    """Return a fail-closed reason until the full local close bundle arrived."""
    manifest = shift.settlement_manifest or {}
    manifest_version = manifest.get('version')
    if (
        manifest_version not in {2, 3}
        or manifest.get('branch_id') != shift.branch_id
    ):
        return 'Close manifest is missing or invalid'

    # Enforce the payment close guard again on the cloud. A rolling older
    # terminal only blocked unpaid OPEN carts, so an unpaid PREPARING/READY
    # order could be omitted from every frozen revenue/tender value while the
    # close manifest still verified. If that order reached the hub before
    # manager handover, fail closed instead of posting an incomplete shift.
    evidence_end = shift.end_time or timezone.now()
    unpaid_count = (
        Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=shift.start_time,
            created_at__lt=evidence_end,
            is_paid=False,
        )
        .exclude(status=Order.Status.CANCELED)
        .count()
    )
    if unpaid_count:
        return (
            f'{unpaid_count} non-cancelled order(s) in the shift are unpaid; '
            'take payment or cancel them before reconciliation'
        )
    tender_error = _shift_tender_integrity_error(shift, evidence_end)
    if tender_error:
        return tender_error
    try:
        manifest_kwargs = {}
        if 'cashier_counted_methods' in manifest:
            manifest_kwargs['cashier_counted_methods'] = manifest.get(
                'cashier_counted_methods'
            )
        current = _build_settlement_manifest(
            shift,
            settlement_rows,
            version=manifest_version,
            **manifest_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on malformed evidence
        return f'Unable to verify settlement evidence: {exc}'
    if current != manifest:
        return (
            'Shift payment, expense, order, payment, refund, or item '
            'evidence does not match the close manifest'
        )

    # SPT fields and the manifest originate at the branch. Recompute expected
    # tenders from the cloud's order/payment/refund/expense evidence before any
    # signed reversal or positive SAFE movement is trusted.
    try:
        from cashbox.services.drawer import expected_payment_totals
        canonical = expected_payment_totals(shift)
    except Exception as exc:  # noqa: BLE001
        return f'Unable to recompute authoritative settlement: {exc}'
    frozen = {
        row.method: _money(row.expected_amount) for row in settlement_rows
    }
    canonical = {
        str(method).upper(): _money(amount)
        for method, amount in canonical.items()
    }
    if any(value is None for value in frozen.values()) or any(
        value is None for value in canonical.values()
    ):
        return 'Settlement contains an invalid money amount'
    # Exact method set is intentional: it detects OrderPayment/refund children
    # that are still behind the Shift/SPT rows in the sync queue.
    if frozen != canonical:
        return 'Cloud order/refund evidence does not match frozen expected tenders'
    return None


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
             date_to=None, live_only=False, actor=None):
        # Join the optional one-to-one and its actor in the page query. Django
        # caches both presence and absence from this outer join, so normal
        # unreconciled shifts neither query per-row nor emit exception logs.
        qs = (ShiftRepository.get_all()
              .select_related(
                  'user', 'shift_template',
                  'reconciliation', 'reconciliation__reconciled_by',
              ))

        qs = _scope_shift_queryset(qs, actor)

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

        if not _actor_can_access_shift(actor, shift):
            return ServiceResponse.forbidden(
                'You cannot view another branch shift'
            )

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
                'treasury_posted_at': (
                    reconciliation.treasury_posted_at.isoformat()
                    if reconciliation.treasury_posted_at else None
                ),
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

        if actor is not None and not _is_global_admin(actor):
            actor_role = str(getattr(actor, 'role', '') or '').upper()
            actor_branch = _effective_actor_branch(actor)
            if actor_branch != operational_branch:
                return ServiceResponse.forbidden(
                    'You cannot start a shift for another branch'
                )
            if actor_role not in ('ADMIN', 'MANAGER') and actor.id != user.id:
                return ServiceResponse.forbidden(
                    'You can only start your own shift'
                )

        active = Shift.objects.filter(
            is_deleted=False,
            user=user,
            status=Shift.Status.ACTIVE,
            end_time__isnull=True,
        ).first()
        if active:
            return ServiceResponse.error("User already has an active shift")

        # DEVICE_ID is minted once per desktop install and is already used by
        # sync presence. Persist it only for CASHIER shifts: managers, admins,
        # and waiters may work alongside the cashier on the same physical till
        # without taking its exclusive cash-drawer slot. Cloud-created and
        # pre-upgrade shifts stay blank for rolling-upgrade compatibility.
        device_id = ''
        if user.role == User.RoleChoices.CASHIER and mode != 'cloud':
            device_id = str(getattr(settings, 'DEVICE_ID', '') or '').strip()
            if len(device_id) > Shift._meta.get_field('device_id').max_length:
                return ServiceResponse.error(
                    'This terminal has an invalid device identity',
                )
        if device_id and Shift.objects.filter(
            is_deleted=False,
            device_id=device_id,
            status=Shift.Status.ACTIVE,
            end_time__isnull=True,
        ).exists():
            return ServiceResponse.error(
                'This terminal already has an active cashier shift',
            )

        kwargs = {
            'user_id': user_id,
            'start_time': timezone.now(),
            'status': 'ACTIVE',
            'branch_id': operational_branch,
            'device_id': device_id,
            # Explicit opt-in proves this shift began under the reconciliation
            # -> SAFE lifecycle. Model default stays fail-closed for late syncs
            # from pre-upgrade/offline clients that do not send this field.
            'treasury_settlement_eligible': True,
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
            # Different users lock different User rows. The conditional device
            # unique constraint is therefore the definitive concurrent-start
            # guard; re-read after the savepoint rollback to return the useful
            # error rather than misreporting it as a per-user duplicate.
            if device_id and Shift.objects.filter(
                is_deleted=False,
                device_id=device_id,
                status=Shift.Status.ACTIVE,
                end_time__isnull=True,
            ).exists():
                return ServiceResponse.error(
                    'This terminal already has an active cashier shift',
                )
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
        if not _actor_can_access_shift(actor, shift):
            return ServiceResponse.forbidden(
                'You cannot end another branch shift'
            )
        if shift.status != 'ACTIVE':
            return ServiceResponse.error("Shift is not active")

        # Every non-cancelled UNPAID sale taken by this cashier must be resolved
        # before handover, regardless of kitchen state. PREPARING/READY describe
        # fulfilment, not settlement: allowing an unpaid READY order through makes
        # it disappear from the frozen revenue/tender totals. Paid kitchen orders
        # still never block; their money is attributed below by paid_at.
        now = timezone.now()

        blocking = Order.objects.filter(
            is_deleted=False,
            cashier_id=shift.user_id,
            branch_id=shift.branch_id,
            created_at__gte=shift.start_time,
            created_at__lt=now,
            is_paid=False,
        ).exclude(status=Order.Status.CANCELED).count()
        if blocking:
            return ServiceResponse.error(
                f"Cannot close shift while {blocking} non-cancelled order(s) are unpaid. "
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
        tender_error = _shift_tender_integrity_error(shift, now)
        if tender_error:
            return ServiceResponse.error(tender_error)
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
        # A post-upgrade shift may become ENDED only when its complete settlement
        # bundle was frozen atomically. Legacy shifts retain the old best-effort
        # behavior so a pre-upgrade offline till can still close. The savepoint
        # prevents a failed child write from leaving a partial settlement behind.
        try:
            with transaction.atomic():
                from cashbox.services.drawer import expected_payment_totals
                from cashbox.models import ShiftPaymentTotal
                counted = counted if isinstance(counted, dict) else {}
                cashier_counted_methods = set()
                frozen_rows = []
                for method, exp in expected_payment_totals(shift).items():
                    raw = counted.get(method)
                    try:
                        cnt = Decimal(str(raw)) if raw is not None else Decimal('0')
                        if raw is not None:
                            cashier_counted_methods.add(method)
                    except (InvalidOperation, TypeError, ValueError):
                        cnt = Decimal('0')
                    row, _ = ShiftPaymentTotal.objects.update_or_create(
                        shift=shift, method=method,
                        defaults={'expected_amount': exp, 'counted_amount': cnt,
                                  'difference': cnt - exp,
                                  'branch_id': shift.branch_id},
                    )
                    frozen_rows.append(row)
                # Publish the close handshake only after every tender row was
                # persisted successfully in this savepoint. If any write fails,
                # rows and manifest roll back together; an eligible close also
                # rolls back its ENDED transition below.
                shift.settlement_manifest = _build_settlement_manifest(
                    shift,
                    frozen_rows,
                    cashier_counted_methods=cashier_counted_methods,
                )
                shift.save(update_fields=[
                    'settlement_manifest', 'synced_at', 'sync_version',
                ])
        except Exception:
            logger.exception(
                'shift settlement write failed (shift=%s)',
                shift.id)
            if shift.treasury_settlement_eligible:
                transaction.set_rollback(True)
                return ServiceResponse.error(
                    'Cannot close shift because its settlement evidence could '
                    'not be frozen. No shift totals were finalized; retry after '
                    'the payment service is healthy.'
                )

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
    def reconcile(shift_id, actual_cash, notes, reconciled_by_id, confirmed=None,
                  actor=None):
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

        if actor is None:
            actor = User.objects.filter(
                pk=reconciled_by_id, is_deleted=False,
            ).first()
        if actor is None or actor.id != reconciled_by_id:
            return ServiceResponse.forbidden('Invalid reconciliation actor')
        actor_role = str(getattr(actor, 'role', '') or '').upper()
        actor_branch = str(getattr(actor, 'branch_id', '') or '').strip()
        is_global_admin = (
            actor_role == 'ADMIN' and actor_branch.lower() in ('', 'cloud')
        )
        if actor_role not in ('ADMIN', 'MANAGER') or (
            not is_global_admin and actor_branch != str(shift.branch_id or '')
        ):
            return ServiceResponse.forbidden(
                'You cannot reconcile another branch shift'
            )

        # Re-checked AFTER acquiring the lock. An exact retry is deliberately
        # idempotent: it returns the frozen audit plus the original treasury
        # entry ids instead of failing or crediting SAFE twice.
        existing = CashReconciliationRepository.get_for_shift(shift_id)
        if existing is None and shift.status != 'ENDED':
            return ServiceResponse.error("Shift must be ended before reconciling")

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
        if existing is None and shift.treasury_settlement_eligible:
            bundle_error = _settlement_bundle_error(shift, settlement_rows)
            if bundle_error:
                return ServiceResponse.validation_error(
                    errors={
                        'code': 'SETTLEMENT_SYNC_INCOMPLETE',
                        'settlement': bundle_error,
                    },
                    message=(
                        'Shift settlement evidence is not ready; sync every '
                        'payment, refund, expense, and tender row before retrying'
                    ),
                )
        _spt_cash = next(
            (row for row in settlement_rows if row.method == 'CASH'), None,
        )
        if existing is not None:
            expected_cash = existing.expected_cash
        elif _spt_cash is not None:
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

        if existing is None and shift.treasury_settlement_eligible:
            missing = sorted(
                row.method for row in settlement_rows
                if row.method != 'CASH'
                and (
                    _money(row.expected_amount) != Decimal('0.00')
                    or _money(row.counted_amount) != Decimal('0.00')
                )
                and row.method not in normalized_confirmed
            )
            if missing:
                return ServiceResponse.validation_error(
                    errors={
                        'confirmed': (
                            'Explicit manager confirmation required for: '
                            + ', '.join(missing)
                        ),
                        'missing_methods': missing,
                    },
                    message='Confirmation is incomplete',
                )

        confirmation_amounts = {}
        for row in settlement_rows:
            # actual_cash is the manager's physical CASH count and therefore is
            # canonical for CASH. Other tenders keep the cashier-count default
            # for a first reconciliation. An idempotent retry defaults to the
            # already-frozen manager values, never to mutable counted figures.
            if existing is not None:
                default = (
                    existing.actual_cash
                    if row.method == 'CASH' and not row.pk
                    else row.confirmed_amount
                )
                raw = normalized_confirmed.get(row.method, default)
            else:
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

        # Manager confirmations describe what is physically handed over and
        # therefore remain non-negative. A refund-only/net-negative tender is a
        # signed economic movement, however: posting the manager's zero count
        # would leave the earlier sale permanently overstated in SAFE. Preserve
        # the exact negative expected movement as an explicit ledger reversal;
        # positive/zero expected tenders post the manager-confirmed amount.
        treasury_amounts = {
            row.method: (
                row.expected_amount
                if row.expected_amount < 0
                else confirmation_amounts[row.method]
            )
            for row in settlement_rows
        }

        if existing is not None:
            if actual != existing.actual_cash:
                return ServiceResponse.validation_error(
                    errors={
                        'actual_cash':
                            'Does not match the completed reconciliation',
                    },
                    message='Conflicting reconciliation retry',
                )
            conflicts = [
                row.method for row in settlement_rows
                if row.pk and confirmation_amounts[row.method] != row.confirmed_amount
            ]
            if conflicts:
                return ServiceResponse.validation_error(
                    errors={
                        'confirmed': (
                            'Does not match the completed reconciliation for: '
                            + ', '.join(sorted(conflicts))
                        ),
                    },
                    message='Conflicting reconciliation retry',
                )

            if existing.treasury_posted_at:
                # This reconciliation was created under the new lifecycle. An
                # exact retry can safely recover/return the database-protected
                # shift+tender postings.
                for spt in settlement_rows:
                    spt.confirmed_amount = confirmation_amounts[spt.method]
                    spt.branch_id = shift.branch_id
                    if spt.pk:
                        spt.save(update_fields=[
                            'confirmed_amount', 'branch_id', 'synced_at',
                            'sync_version',
                        ])
                    else:
                        spt.save()

                from base.services.treasury_service import TreasuryService
                treasury_posting = TreasuryService.post_shift_settlement(
                    shift.id,
                    treasury_amounts,
                    performed_by=existing.reconciled_by,
                    branch_id=shift.branch_id,
                )
            else:
                # Historical reconciliations may already have reached treasury
                # through the old Inkassa recognition path. There is no safe
                # shift-level linkage to prove otherwise, so never auto-backfill
                # them on a retry (that could double-credit real money).
                treasury_posting = ShiftService._shift_treasury_posting(shift)
                treasury_posting['reason'] = 'LEGACY_RECONCILIATION_NOT_REPOSTED'
            return ServiceResponse.success(data={
                'id': existing.id,
                'shift_id': shift.id,
                'expected_cash': str(existing.expected_cash),
                'actual_cash': str(existing.actual_cash),
                'difference': str(existing.difference),
                'notes': existing.notes,
                'reconciled_by_id': existing.reconciled_by_id,
                'created_at': (
                    existing.created_at.isoformat()
                    if existing.created_at else None
                ),
                'treasury_posted_at': (
                    existing.treasury_posted_at.isoformat()
                    if existing.treasury_posted_at else None
                ),
                'settlement': ShiftService._shift_settlement(shift),
                'treasury_posting': treasury_posting,
            }, message='Reconciliation already completed')

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

        # Freeze the manager's per-tender confirmations. This is now the one
        # authoritative treasury-recognition boundary: ALL tenders go to SAFE.
        # A later inkassa may physically remove drawer cash, but is audit/
        # transport only and must not create another treasury credit.
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

        if shift.treasury_settlement_eligible:
            from base.services.treasury_service import TreasuryService
            treasury_posting = TreasuryService.post_shift_settlement(
                shift.id,
                treasury_amounts,
                performed_by=reconciliation.reconciled_by,
                branch_id=shift.branch_id,
            )
            reconciliation.treasury_posted_at = timezone.now()
            reconciliation.save(update_fields=[
                'treasury_posted_at', 'synced_at', 'sync_version',
            ])
        else:
            # This shift was already closed before the lifecycle rollout. Its
            # receipts may have been recognized by legacy Inkassa and there is
            # no shift-level link that can prove otherwise. Preserve the audit
            # confirmation, but never risk a second treasury credit.
            treasury_posting = ShiftService._shift_treasury_posting(shift)
            treasury_posting['reason'] = 'LEGACY_SHIFT_NOT_ELIGIBLE'

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
            'treasury_posted_at': (
                reconciliation.treasury_posted_at.isoformat()
                if reconciliation.treasury_posted_at else None
            ),
            # Per-tender cashier-vs-manager audit comparison.
            'settlement': ShiftService._shift_settlement(shift),
            'treasury_posting': treasury_posting,
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
    def get_active_shifts(actor=None):
        qs = _scope_shift_queryset(
            ShiftRepository.filter_by_status('ACTIVE'), actor,
        )
        shifts = list(qs
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
        reconciled = CashReconciliation.objects.filter(
            shift=shift, is_deleted=False,
        ).exists()
        return [{
            'method': r.method,
            'expected': str(r.expected_amount),
            'counted': str(r.counted_amount),      # cashier
            'confirmed': str(r.confirmed_amount),  # manager
            'difference': str(r.difference),
            'status': _settlement_row_status(
                shift, r, reconciled=reconciled,
            ),
            'reconciled': reconciled,
        } for r in rows]

    @staticmethod
    def _serialize_cashbox_expense(expense):
        """Stable shift/detail expense contract over CashboxExpense evidence."""
        from cashbox.models import CashboxExpense
        created_at = (
            expense.created_at.isoformat() if expense.created_at else None
        )
        paid_by = None
        if expense.created_by:
            paid_by = {
                'id': expense.created_by.id,
                'name': (
                    f'{expense.created_by.first_name} '
                    f'{expense.created_by.last_name}'
                ).strip(),
            }
        description = CashboxExpense.visible_comment(expense.comment)
        return {
            'id': expense.id,
            'shift_id': expense.shift_id,
            'amount': str(expense.amount),
            'category': expense.category.name if expense.category else None,
            'category_id': expense.category_id,
            # Both names are kept because cashbox endpoints historically use
            # comment while the manager handover contract calls it description.
            'description': description,
            'comment': description,
            'paid_at': created_at,
            'created_at': created_at,
            'paid_by': paid_by,
            # The model is durable evidence, not an approval workflow. Remote
            # drawer application is tracked cumulatively and cannot provide an
            # honest per-row pending flag, so RECORDED is the precise status.
            'status': 'RECORDED',
        }

    @staticmethod
    def _shift_treasury_posting(shift):
        """Read-only authoritative posting state for a shift detail response."""
        from base.models import TreasuryTransaction
        rows = list(
            TreasuryTransaction.objects.filter(
                type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
                reference_type='ShiftSettlement',
                reference_id=shift.id,
                account__kind='SAFE',
                account__is_deleted=False,
            ).order_by('category', 'id')
        )
        total = sum((row.delta for row in rows), Decimal('0.00'))
        zero_posted = (
            not rows
            and CashReconciliation.objects.filter(
                shift=shift,
                is_deleted=False,
                treasury_posted_at__isnull=False,
            ).exists()
        )
        posting = {
            'status': 'posted' if rows or zero_posted else 'not_posted',
            'account': 'SAFE',
            'total': str(total.quantize(_MONEY_QUANTUM)),
            'tenders': [
                {'method': row.category, 'amount': str(row.delta)}
                for row in rows
            ],
            'entry_ids': [row.id for row in rows],
        }
        if not rows:
            if not shift.treasury_settlement_eligible:
                posting['reason'] = 'LEGACY_SHIFT_NOT_ELIGIBLE'
            elif zero_posted:
                posting['reason'] = 'ZERO_SETTLEMENT'
        return posting

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
        from cashbox.models import (
            CashboxExpense, PAYMENT_METHODS, ShiftPaymentTotal,
        )

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
                'expected_by_tender': {},
                'total_expected_to_receive': '0.00',
                'settlement': [],
                'reconciled_count': 0,
                'cashbox_expenses': [],
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
                _drawer_cash_from_sources,
                _courier_rows_by_order,
                split_from_rows,
            )
            mix_acc = defaultdict(
                lambda: {'cash': zero, 'card': zero, 'payme': zero, 'unknown': zero})
            tender_acc = defaultdict(lambda: defaultdict(lambda: zero))
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
                order_payments = _ops.get(oid, ())
                courier_payments = _courier.get(oid, ())
                _s, _detail = split_from_rows(
                    amt,
                    method,
                    order_payments,
                    courier_payments,
                    order_id=oid,
                )
                acc = mix_acc[sid]
                for _k in ('cash', 'card', 'payme', 'unknown'):
                    acc[_k] += _s[_k]
                tender = tender_acc[sid]
                tender['CASH'] += _drawer_cash_from_sources(
                    amt, _s, order_payments, courier_payments,
                )
                tender['PAYME'] += _s['payme']
                for tender_method, tender_amount in _detail.items():
                    tender[tender_method] += tender_amount
                tender['UNKNOWN'] += _s['unknown']
                paid_cnt[sid] += 1

            refund_cnt = defaultdict(int)
            refund_total = defaultdict(lambda: Decimal('0.00'))
            for (
                sid, row_branch, amount, cash, drawer_cash, card, payme,
                unknown, card_detail,
            ) in OrderRefund.objects.filter(
                is_deleted=False,
                shift_id__in=list(out.keys()),
                branch_id__in=branch_ids,
            ).values_list(
                'shift_id', 'branch_id', 'amount', 'cash_amount',
                'drawer_cash_amount', 'card_amount', 'payme_amount',
                'unknown_amount', 'card_detail',
            ):
                if shift_branches.get(sid) != row_branch:
                    continue
                acc = mix_acc[sid]
                acc['cash'] -= cash or zero
                acc['card'] -= card or zero
                acc['payme'] -= payme or zero
                acc['unknown'] -= unknown or zero
                tender = tender_acc[sid]
                tender['CASH'] -= drawer_cash or zero
                tender['PAYME'] -= payme or zero
                tender['UNKNOWN'] -= unknown or zero
                for tender_method, tender_amount in (card_detail or {}).items():
                    tender[str(tender_method).upper()] -= Decimal(
                        str(tender_amount or 0)
                    )
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

            expense_items = defaultdict(list)
            for expense in (
                CashboxExpense.objects.filter(
                    shift_id__in=list(out.keys()),
                    branch_id__in=branch_ids,
                    is_deleted=False,
                )
                .select_related('category', 'created_by')
                .order_by('shift_id', '-created_at', '-id')
            ):
                if shift_branches.get(expense.shift_id) != expense.branch_id:
                    continue
                expense_items[expense.shift_id].append(
                    ShiftService._serialize_cashbox_expense(expense)
                )

            settlement_rows = defaultdict(list)
            for row in ShiftPaymentTotal.objects.filter(
                shift_id__in=list(out.keys()),
                branch_id__in=branch_ids,
                is_deleted=False,
            ).order_by('shift_id', 'method'):
                if shift_branches.get(row.shift_id) != row.branch_id:
                    continue
                settlement_rows[row.shift_id].append(row)

            reconciled_shift_ids = set(
                CashReconciliation.objects.filter(
                    shift_id__in=list(out.keys()),
                    is_deleted=False,
                ).values_list('shift_id', flat=True)
            )
            shifts_by_id = {shift.id: shift for shift in valid}

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
                frozen = settlement_rows.get(sid, [])
                reconciled = sid in reconciled_shift_ids
                settlement = [{
                    'method': row.method,
                    'expected': money(row.expected_amount),
                    'counted': money(row.counted_amount),
                    'confirmed': money(row.confirmed_amount),
                    'difference': money(row.difference),
                    'status': _settlement_row_status(
                        shifts_by_id[sid], row, reconciled=reconciled,
                    ),
                    'reconciled': reconciled,
                } for row in frozen]
                expected_by_tender = {
                    row.method: money(row.expected_amount) for row in frozen
                }
                if not expected_by_tender:
                    # Active/legacy fallback from the same source rows as the
                    # canonical drawer engine. Keep acquirer identities instead
                    # of collapsing HUMO/UZCARD/CARD into a generic card bucket.
                    exact = tender_acc.get(sid, {})
                    expected_by_tender = {
                        method: money(
                            exact.get(method, zero)
                            - (exp_total.get(sid, zero) if method == 'CASH' else zero)
                        )
                        for method in PAYMENT_METHODS
                    }
                    if exact.get('UNKNOWN'):
                        expected_by_tender['UNKNOWN'] = money(exact['UNKNOWN'])
                total_expected = sum(
                    (Decimal(value) for value in expected_by_tender.values()),
                    zero,
                )
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
                    'expected_by_tender': expected_by_tender,
                    'total_expected_to_receive': money(total_expected),
                    'settlement': settlement,
                    'reconciled_count': len(settlement) if reconciled else 0,
                    'cashbox_expenses': expense_items.get(sid, []),
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
        # List/active callers already pass the O(1) batched map. Standalone
        # detail/current/end responses used to omit it and therefore emitted
        # card_collected=0/payme_collected=0 even when authoritative payment
        # rows existed (the production shift-47 symptom). Compute the same
        # canonical map for the one row instead of falling back to the rolled-up
        # Order.payment_method or a duplicate tender implementation.
        if extras is None and shift.start_time:
            extras = ShiftService._batch_list_extras([shift], now=now).get(shift.id)
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
                    'treasury_posted_at': (
                        rec.treasury_posted_at.isoformat()
                        if rec.treasury_posted_at else None
                    ),
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
            'device_id': shift.device_id or None,
            'treasury_settlement_eligible': shift.treasury_settlement_eligible,
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
            result['treasury_posting'] = ShiftService._shift_treasury_posting(shift)
        return result
