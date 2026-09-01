from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import TreasuryTransaction
from base.money import MoneyValueError, local_iso, uzs_int, whole_uzs
from base.services.branch_scope import resolve_actor_branch
from stock.models import (
    PurchaseOrder,
    PurchaseReceiving,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
    SupplierTransaction,
)


_DEBT_MINUS = {
    SupplierTransaction.Type.PAYMENT,
    SupplierTransaction.Type.RETURN,
}


def _actor_name(actor):
    if actor is None:
        return ''
    return f'{actor.first_name} {actor.last_name}'.strip()


def _branch(actor=None, branch_id=None):
    return str(branch_id or resolve_actor_branch(actor) or '').strip()


def _serialize_ledger(row):
    decreases_debt = row.type in _DEBT_MINUS
    return {
        'id': row.id,
        'type': row.type,
        'transaction_type': row.type,
        'amount_uzs': uzs_int(row.amount),
        'principal_uzs': uzs_int(row.amount),
        'payable_increase_uzs': 0 if decreases_debt else uzs_int(row.amount),
        'payable_decrease_uzs': uzs_int(row.amount) if decreases_debt else 0,
        'change_uzs': -uzs_int(row.amount) if decreases_debt else uzs_int(row.amount),
        'balance_before_uzs': uzs_int(row.balance_before),
        'balance_after_uzs': uzs_int(row.balance_after),
        'source_account': row.source_account or None,
        'fee_uzs': uzs_int(row.fee),
        'reference_type': row.reference_type or None,
        'reference_id': row.reference_id,
        'note': row.note,
        'performed_by': ({
            'id': row.performed_by_id,
            'name': _actor_name(row.performed_by),
        } if row.performed_by_id else None),
        'created_at': local_iso(row.created_at),
    }


class SupplierLedgerService:
    @classmethod
    def _post_locked(cls, supplier, txn_type, amount, *, source_account='', fee=0,
                     reference_type='', reference_id=None, note='',
                     performed_by=None):
        amount = Decimal(amount)
        fee = Decimal(fee)
        before = supplier.current_balance or Decimal('0')
        after = before - amount if txn_type in _DEBT_MINUS else before + amount
        row = SupplierTransaction.objects.create(
            supplier=supplier,
            type=txn_type,
            amount=amount,
            balance_before=before,
            balance_after=after,
            source_account=source_account or '',
            fee=fee,
            note=str(note or ''),
            reference_type=str(reference_type or ''),
            reference_id=reference_id,
            performed_by=performed_by,
            branch_id=supplier.branch_id,
        )
        supplier.current_balance = after
        supplier.save(update_fields=[
            'current_balance', 'updated_at', 'synced_at', 'sync_version',
        ])
        return row

    @classmethod
    @transaction.atomic
    def _post(cls, supplier_id, txn_type, amount, *, source_account='', fee=0,
              reference_type='', reference_id=None, note='', performed_by=None):
        supplier = Supplier.objects.select_for_update().filter(
            pk=supplier_id,
            is_deleted=False,
        ).first()
        if supplier is None:
            return None
        return cls._post_locked(
            supplier,
            txn_type,
            amount,
            source_account=source_account,
            fee=fee,
            reference_type=reference_type,
            reference_id=reference_id,
            note=note,
            performed_by=performed_by,
        )

    @classmethod
    def record_purchase(cls, supplier_id, amount, reference_type='',
                        reference_id=None, performed_by=None, note=''):
        return cls._post(
            supplier_id,
            SupplierTransaction.Type.PURCHASE,
            amount,
            reference_type=reference_type,
            reference_id=reference_id,
            performed_by=performed_by,
            note=note,
        )

    @classmethod
    def record_return(cls, supplier_id, amount, reference_type='',
                      reference_id=None, performed_by=None, note=''):
        return cls._post(
            supplier_id,
            SupplierTransaction.Type.RETURN,
            amount,
            reference_type=reference_type,
            reference_id=reference_id,
            performed_by=performed_by,
            note=note,
        )

    @staticmethod
    def record_purchase_order_payment(*_args, **_kwargs):
        return None

    @staticmethod
    def record_drawer_payment(*_args, **_kwargs):
        return None

    @classmethod
    def pay_supplier(cls, supplier_id, amount=None, source_account='SAFE',
                     commission=0, note='', performed_by=None, *, amount_uzs=None,
                     fee_uzs=None, allocation_mode='AUTO_OLDEST_DUE',
                     allocations=None, action_id=None, idempotency_key='',
                     request_hash='', branch_id=None):
        return SupplierPaymentService.pay(
            supplier_id=supplier_id,
            amount_uzs=amount_uzs if amount_uzs is not None else amount,
            source_account=source_account,
            fee_uzs=fee_uzs if fee_uzs is not None else commission,
            allocation_mode=allocation_mode,
            allocations=allocations,
            note=note,
            actor=performed_by,
            action_id=action_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            branch_id=branch_id,
        )

    @staticmethod
    def history(supplier_id, page=1, per_page=20, *, actor=None, branch_id=None,
                date_from=None, date_to=None, transaction_type=None,
                source_account=None, reference_type=None, reference_id=None,
                search=None, source_reference=None):
        branch_id = _branch(actor, branch_id)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Supplier branch could not be resolved.', 403,
            )
        supplier = Supplier.objects.filter(
            pk=supplier_id,
            branch_id=branch_id,
            is_deleted=False,
        ).first()
        if supplier is None:
            return ServiceResponse.not_found('Supplier not found')
        queryset = SupplierTransaction.objects.filter(
            supplier=supplier,
            branch_id=branch_id,
            is_deleted=False,
        ).select_related('performed_by')
        if transaction_type:
            transaction_type = str(transaction_type).upper()
            if transaction_type not in SupplierTransaction.Type.values:
                return ServiceResponse.validation_error({
                    'type': ['Unknown supplier transaction type.'],
                })
            queryset = queryset.filter(type=transaction_type)
        if source_account:
            source_account = str(source_account).upper()
            if source_account not in SupplierTransaction.SourceAccount.values:
                return ServiceResponse.validation_error({
                    'source_account': ['Must be DRAWER, SAFE, or BANK.'],
                })
            queryset = queryset.filter(source_account=source_account)
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        if reference_type:
            queryset = queryset.filter(reference_type=reference_type)
        if reference_id is not None:
            queryset = queryset.filter(reference_id=reference_id)
        if source_reference and not search:
            search = source_reference
        if search:
            term = str(search).strip()
            condition = (
                Q(note__icontains=term)
                | Q(reference_type__icontains=term)
                | Q(performed_by__first_name__icontains=term)
                | Q(performed_by__last_name__icontains=term)
            )
            if term.isdigit():
                condition |= Q(reference_id=int(term))
            queryset = queryset.filter(condition)
        totals = queryset.aggregate(
            principal=Sum('amount'),
            payable_increase=Sum(
                'amount',
                filter=~Q(type__in=_DEBT_MINUS),
            ),
            payable_decrease=Sum('amount', filter=Q(type__in=_DEBT_MINUS)),
        )
        total = queryset.count()
        rows = queryset.order_by('-created_at', '-id')[
            (page - 1) * per_page:page * per_page
        ]
        return ServiceResponse.success(data={
            'supplier': {
                'id': supplier.id,
                'name': supplier.name,
                'currency': supplier.currency,
            },
            'current_balance_uzs': (
                uzs_int(supplier.current_balance)
                if supplier.currency == 'UZS' else None
            ),
            'transactions': [_serialize_ledger(row) for row in rows],
            'totals': {
                'principal_uzs': uzs_int(totals['principal'] or 0),
                'payable_increase_uzs': uzs_int(totals['payable_increase'] or 0),
                'payable_decrease_uzs': uzs_int(totals['payable_decrease'] or 0),
                'row_count': total,
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            },
        })


class SupplierPaymentService:
    @classmethod
    @transaction.atomic
    def pay(cls, supplier_id, amount_uzs, source_account, *, fee_uzs=0,
            allocation_mode='EXPLICIT', allocations=None, note='', actor=None,
            action_id=None, idempotency_key='', request_hash='', branch_id=None):
        branch_id = _branch(actor, branch_id)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Supplier branch could not be resolved.', 403,
            )
        if action_id:
            existing = SupplierPayment.objects.filter(
                payment_action_id=action_id,
                branch_id=branch_id,
            ).select_related(
                'supplier', 'supplier_transaction', 'treasury_transaction',
                'performed_by',
            ).prefetch_related('allocations__purchase_order').first()
            if existing:
                return ServiceResponse.created(data=cls.serialize(existing))
        try:
            principal = whole_uzs(
                amount_uzs,
                'amount_uzs',
                positive=True,
                maximum=Decimal('9999999999999'),
            )
            fee = whole_uzs(
                fee_uzs or 0,
                'fee_uzs',
                maximum=Decimal('9999999999999'),
            )
        except MoneyValueError as exc:
            return ServiceResponse.validation_error({'amount_uzs': [str(exc)]})
        source = str(source_account or '').strip().upper()
        if source not in SupplierPayment.SourceAccount.values:
            return ServiceResponse.validation_error({
                'source_account': ['Must be SAFE or BANK.'],
            })
        if source != SupplierPayment.SourceAccount.BANK and fee:
            return ServiceResponse.failure(
                'FEE_BANK_ONLY',
                'A supplier-payment fee is allowed only for BANK.',
                422,
                errors={'fee_uzs': ['Fee must be zero for SAFE.']},
            )
        mode = str(allocation_mode or '').strip().upper()
        if mode not in {
            SupplierPayment.AllocationMode.EXPLICIT,
            SupplierPayment.AllocationMode.AUTO_OLDEST_DUE,
        }:
            return ServiceResponse.validation_error({
                'allocation_mode': ['Must be EXPLICIT or AUTO_OLDEST_DUE.'],
            })

        supplier = Supplier.objects.select_for_update().filter(
            pk=supplier_id,
            branch_id=branch_id,
            is_deleted=False,
        ).first()
        if supplier is None:
            return ServiceResponse.not_found('Supplier not found')
        if action_id:
            existing = SupplierPayment.objects.filter(
                payment_action_id=action_id,
                branch_id=branch_id,
            ).select_related(
                'supplier', 'supplier_transaction', 'treasury_transaction',
                'performed_by',
            ).prefetch_related('allocations__purchase_order').first()
            if existing:
                return ServiceResponse.created(data=cls.serialize(existing))
        if supplier.currency != 'UZS':
            return ServiceResponse.failure(
                'SUPPLIER_CURRENCY_UNSUPPORTED',
                'Supplier payment currency must be UZS.',
                422,
                details={'currency': supplier.currency},
            )
        payable = supplier.current_balance or Decimal('0')
        if payable <= 0 or principal > payable:
            return ServiceResponse.failure(
                'SUPPLIER_PAYMENT_EXCEEDS_PAYABLE',
                'Payment cannot exceed the supplier payable.',
                422,
                errors={'amount_uzs': ['Amount exceeds positive payable.']},
                details={
                    'payable_uzs': uzs_int(max(payable, Decimal('0'))),
                    'requested_uzs': uzs_int(principal),
                },
            )

        parsed_allocations, allocation_error = cls._parse_allocations(
            supplier,
            principal,
            mode,
            allocations,
        )
        if allocation_error:
            return allocation_error

        from base.services.treasury_service import TreasuryService

        treasury_result, treasury_status = TreasuryService.record_expense(
            account_kind=source,
            amount=principal,
            fee=fee,
            performed_by=actor,
            txn_type=TreasuryTransaction.Type.SUPPLIER_PAYMENT,
            reference_type='Supplier',
            reference_id=supplier.id,
            description=note or f'Supplier payment: {supplier.name}',
            branch_id=branch_id,
            command_id=action_id,
            idempotency_key=idempotency_key,
        )
        if treasury_status >= 400:
            transaction.set_rollback(True)
            return treasury_result, treasury_status
        treasury_id = treasury_result['data']['transaction']['id']
        treasury_row = TreasuryTransaction.objects.get(pk=treasury_id)
        supplier_row = SupplierLedgerService._post_locked(
            supplier,
            SupplierTransaction.Type.PAYMENT,
            principal,
            source_account=source,
            fee=fee,
            reference_type='Supplier',
            reference_id=supplier.id,
            note=note,
            performed_by=actor,
        )
        payment = SupplierPayment.objects.create(
            branch_id=branch_id,
            supplier=supplier,
            principal_uzs=principal,
            fee_uzs=fee,
            total_debited_uzs=principal + fee,
            source_account=source,
            allocation_mode=mode,
            status=SupplierPayment.Status.POSTED,
            supplier_balance_before_uzs=supplier_row.balance_before,
            supplier_balance_after_uzs=supplier_row.balance_after,
            source_balance_before_uzs=treasury_row.balance_before,
            source_balance_after_uzs=treasury_row.balance_after,
            treasury_transaction=treasury_row,
            supplier_transaction=supplier_row,
            payment_action_id=action_id,
            idempotency_key=idempotency_key or '',
            request_hash=request_hash or '',
            note=str(note or ''),
            performed_by=actor,
            actor_display_snapshot=_actor_name(actor),
        )
        for po, amount in parsed_allocations:
            SupplierPaymentAllocation.objects.create(
                payment=payment,
                purchase_order=po,
                amount_uzs=amount,
                payment_status_snapshot=PurchaseOrder.PaymentStatus.UNPAID,
                remaining_uzs_snapshot=Decimal('0'),
            )
            cls._refresh_purchase_order(po)
            allocation = payment.allocations.get(purchase_order=po)
            allocation.payment_status_snapshot = po.payment_status
            allocation.remaining_uzs_snapshot = max(
                cls._received_principal(po) - po.amount_paid,
                Decimal('0'),
            )
            allocation.save(update_fields=[
                'payment_status_snapshot', 'remaining_uzs_snapshot',
            ])
        payment = SupplierPayment.objects.select_related(
            'supplier', 'supplier_transaction', 'treasury_transaction',
            'performed_by',
        ).prefetch_related('allocations__purchase_order').get(pk=payment.pk)
        return ServiceResponse.created(data=cls.serialize(payment))

    @classmethod
    @transaction.atomic
    def reverse(cls, payment_id, *, actor, reason, action_id=None,
                idempotency_key='', branch_id=None, supplier_id=None):
        branch_id = _branch(actor, branch_id)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED',
                'Supplier branch could not be resolved.',
                403,
            )
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error({
                'reason': ['This field is required.'],
            })
        candidate = SupplierPayment.objects.filter(
            pk=payment_id,
            branch_id=branch_id,
        ).values('supplier_id').first()
        if candidate is None or (
            supplier_id is not None
            and candidate['supplier_id'] != supplier_id
        ):
            return ServiceResponse.not_found('Supplier payment not found')
        Supplier.objects.select_for_update().get(
            pk=candidate['supplier_id'],
            branch_id=branch_id,
            is_deleted=False,
        )
        payment = SupplierPayment.objects.select_for_update(of=('self',)).select_related(
            'supplier', 'supplier_transaction', 'treasury_transaction',
            'treasury_reversal', 'supplier_reversal', 'performed_by',
            'reversed_by',
        ).get(pk=payment_id)
        if payment.status == SupplierPayment.Status.REVERSED:
            if action_id and payment.reversal_action_id == action_id:
                return ServiceResponse.success(data=cls.serialize(payment))
            return ServiceResponse.conflict(
                'SUPPLIER_PAYMENT_ALREADY_REVERSED',
                'This supplier payment is already reversed.',
                details={'payment_id': payment.id},
            )
        if payment.status != SupplierPayment.Status.POSTED:
            return ServiceResponse.conflict(
                'SUPPLIER_PAYMENT_STATE_CONFLICT',
                'Only a posted supplier payment can be reversed.',
                details={'payment_id': payment.id, 'status': payment.status},
            )
        if payment.treasury_transaction_id is None:
            return ServiceResponse.conflict(
                'PURCHASE_PAYMENT_WITHOUT_FUNDING_SOURCE',
                'This payment has no proven Treasury funding source.',
                details={'payment_id': payment.id},
            )

        purchase_order_ids = list(
            SupplierPaymentAllocation.objects.filter(payment=payment)
            .order_by('purchase_order_id')
            .values_list('purchase_order_id', flat=True)
        )
        purchase_orders = list(
            PurchaseOrder.objects.select_for_update()
            .filter(pk__in=purchase_order_ids)
            .order_by('pk')
        )
        from base.services.treasury_service import TreasuryService

        treasury_result, treasury_status = TreasuryService.reverse_transaction(
            payment.treasury_transaction_id,
            performed_by=actor,
            reason=reason,
            branch_id=branch_id,
            command_id=action_id,
            transaction_type=(
                TreasuryTransaction.Type.SUPPLIER_PAYMENT_REVERSAL
            ),
            idempotency_key=idempotency_key,
        )
        if treasury_status >= 400:
            transaction.set_rollback(True)
            return treasury_result, treasury_status
        treasury_reversal = TreasuryTransaction.objects.get(
            pk=treasury_result['data']['transaction']['id'],
        )
        supplier_reversal = SupplierLedgerService._post_locked(
            payment.supplier,
            SupplierTransaction.Type.PAYMENT_REVERSAL,
            payment.principal_uzs,
            source_account=payment.source_account,
            fee=-payment.fee_uzs,
            reference_type='SupplierPayment',
            reference_id=payment.id,
            note=reason,
            performed_by=actor,
        )
        payment.status = SupplierPayment.Status.REVERSED
        payment.reversed_at = timezone.now()
        payment.reversed_by = actor
        payment.reversal_reason = reason
        payment.reversal_action_id = action_id
        payment.reversal_idempotency_key = idempotency_key or ''
        payment.reversed_actor_display_snapshot = _actor_name(actor)
        payment.treasury_reversal = treasury_reversal
        payment.supplier_reversal = supplier_reversal
        payment.save(update_fields=[
            'status', 'reversed_at', 'reversed_by', 'reversal_reason',
            'reversal_action_id', 'reversal_idempotency_key',
            'reversed_actor_display_snapshot', 'treasury_reversal',
            'supplier_reversal', 'updated_at',
        ])
        for po in purchase_orders:
            cls._refresh_purchase_order(po)
        payment = SupplierPayment.objects.select_related(
            'supplier', 'supplier_transaction', 'treasury_transaction',
            'treasury_reversal', 'supplier_reversal', 'performed_by',
            'reversed_by',
        ).prefetch_related('allocations__purchase_order').get(pk=payment.pk)
        return ServiceResponse.success(data=cls.serialize(payment))

    @classmethod
    def get(cls, payment_id, *, actor=None, branch_id=None,
            supplier_id=None):
        branch_id = _branch(actor, branch_id)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED',
                'Supplier branch could not be resolved.',
                403,
            )
        queryset = SupplierPayment.objects.filter(
            pk=payment_id,
            branch_id=branch_id,
        ).select_related(
            'supplier', 'supplier_transaction', 'treasury_transaction',
            'treasury_reversal', 'supplier_reversal', 'performed_by',
            'reversed_by',
        ).prefetch_related('allocations__purchase_order')
        if supplier_id is not None:
            queryset = queryset.filter(supplier_id=supplier_id)
        payment = queryset.first()
        if payment is None:
            return ServiceResponse.not_found('Supplier payment not found')
        return ServiceResponse.success(data=cls.serialize(payment))

    @classmethod
    def _parse_allocations(cls, supplier, principal, mode, allocations):
        if mode == SupplierPayment.AllocationMode.EXPLICIT:
            if not isinstance(allocations, list) or not allocations:
                return None, ServiceResponse.validation_error({
                    'allocations': ['At least one allocation is required.'],
                })
            parsed = {}
            errors = {}
            for index, value in enumerate(allocations):
                try:
                    po_id = int(value.get('purchase_order_id'))
                    if isinstance(value.get('purchase_order_id'), bool) or po_id <= 0:
                        raise ValueError
                except (AttributeError, TypeError, ValueError):
                    errors[f'allocations.{index}.purchase_order_id'] = [
                        'Use a positive integer.',
                    ]
                    continue
                if po_id in parsed:
                    errors[f'allocations.{index}.purchase_order_id'] = [
                        'Each purchase order may appear once.',
                    ]
                    continue
                try:
                    amount = whole_uzs(
                        value.get('amount_uzs'),
                        f'allocations.{index}.amount_uzs',
                        positive=True,
                    )
                except MoneyValueError as exc:
                    errors[f'allocations.{index}.amount_uzs'] = [str(exc)]
                    continue
                parsed[po_id] = amount
            if errors:
                return None, ServiceResponse.validation_error(errors)
            if sum(parsed.values(), Decimal('0')) != principal:
                return None, ServiceResponse.validation_error({
                    'allocations': ['Allocation total must equal amount_uzs.'],
                })
            rows = list(PurchaseOrder.objects.select_for_update().filter(
                pk__in=sorted(parsed),
                supplier=supplier,
                branch_id=supplier.branch_id,
                is_deleted=False,
            ).order_by('pk'))
            if len(rows) != len(parsed):
                return None, ServiceResponse.validation_error({
                    'allocations': ['A purchase order is missing or belongs elsewhere.'],
                })
            output = []
            for po in rows:
                error = cls._allocation_error(po, parsed[po.id])
                if error:
                    return None, error
                output.append((po, parsed[po.id]))
            return output, None

        rows = list(PurchaseOrder.objects.select_for_update().filter(
            supplier=supplier,
            branch_id=supplier.branch_id,
            is_deleted=False,
        ).exclude(
            status=PurchaseOrder.Status.CANCELED,
        ).order_by('pk'))
        rows.sort(key=lambda po: (
            po.payment_due_date is None,
            po.payment_due_date or timezone.now(),
            po.id,
        ))
        remaining = principal
        output = []
        for po in rows:
            canonical_paid = cls._canonical_paid(po)
            if canonical_paid != po.amount_paid:
                continue
            available = max(
                cls._received_principal(po) - canonical_paid,
                Decimal('0'),
            )
            if available <= 0:
                continue
            amount = min(remaining, available)
            output.append((po, amount))
            remaining -= amount
            if remaining == 0:
                break
        if remaining:
            return None, ServiceResponse.failure(
                'SUPPLIER_PAYMENT_ALLOCATION_INCOMPLETE',
                'Open purchase orders cannot fully allocate this payment.',
                422,
                errors={'allocations': ['Unallocated principal remains.']},
                details={'unallocated_uzs': uzs_int(remaining)},
            )
        return output, None

    @classmethod
    def _allocation_error(cls, po, amount):
        if po.currency != 'UZS':
            return ServiceResponse.failure(
                'SUPPLIER_CURRENCY_UNSUPPORTED',
                'Purchase-order currency must be UZS.',
                422,
                details={'purchase_order_id': po.id, 'currency': po.currency},
            )
        canonical_paid = cls._canonical_paid(po)
        if canonical_paid != po.amount_paid:
            return ServiceResponse.conflict(
                'PURCHASE_PAYMENT_WITHOUT_FUNDING_SOURCE',
                'Purchase-order payment history requires reconciliation.',
                details={'purchase_order_id': po.id},
            )
        remaining = max(
            cls._received_principal(po) - canonical_paid,
            Decimal('0'),
        )
        if amount > remaining:
            return ServiceResponse.validation_error({
                'allocations': [
                    f'Allocation for purchase order {po.id} exceeds {uzs_int(remaining)} UZS.'
                ],
            })
        return None

    @staticmethod
    def _canonical_paid(po):
        return po.payment_allocations.filter(
            payment__status__in=[
                SupplierPayment.Status.POSTED,
                SupplierPayment.Status.LEGACY_UNFUNDED,
            ],
        ).aggregate(total=Sum('amount_uzs'))['total'] or Decimal('0')

    @staticmethod
    def _received_principal(po):
        receiving_ids = PurchaseReceiving.objects.filter(
            purchase_order=po,
            status=PurchaseReceiving.Status.COMPLETED,
            is_deleted=False,
        ).values_list('id', flat=True)
        return SupplierTransaction.objects.filter(
            supplier=po.supplier,
            type=SupplierTransaction.Type.PURCHASE,
            reference_type='PurchaseReceiving',
            reference_id__in=receiving_ids,
            is_deleted=False,
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    @classmethod
    def _refresh_purchase_order(cls, po):
        paid = cls._canonical_paid(po)
        po.amount_paid = paid
        if paid <= 0:
            po.payment_status = PurchaseOrder.PaymentStatus.UNPAID
        elif paid >= po.total:
            po.payment_status = PurchaseOrder.PaymentStatus.PAID
        else:
            po.payment_status = PurchaseOrder.PaymentStatus.PARTIAL
        po.save(update_fields=['amount_paid', 'payment_status', 'updated_at'])

    @staticmethod
    def serialize(payment):
        allocations = [{
            'purchase_order_id': row.purchase_order_id,
            'amount_uzs': uzs_int(row.amount_uzs),
            'payment_status': row.payment_status_snapshot,
            'remaining_uzs': uzs_int(row.remaining_uzs_snapshot),
        } for row in payment.allocations.all()]
        actions = [{
            'action': 'POSTED',
            'actor': {
                'id': payment.performed_by_id,
                'name': payment.actor_display_snapshot or _actor_name(payment.performed_by),
            },
            'at': local_iso(payment.paid_at),
            'note': payment.note,
            'treasury_transaction_id': payment.treasury_transaction_id,
            'supplier_transaction_id': payment.supplier_transaction_id,
        }]
        if payment.reversed_at:
            actions.append({
                'action': 'REVERSED',
                'actor': {
                    'id': payment.reversed_by_id,
                    'name': (
                        payment.reversed_actor_display_snapshot
                        or _actor_name(payment.reversed_by)
                    ),
                },
                'at': local_iso(payment.reversed_at),
                'reason': payment.reversal_reason,
                'treasury_transaction_id': payment.treasury_reversal_id,
                'supplier_transaction_id': payment.supplier_reversal_id,
            })
        return {
            'payment_id': payment.id,
            'uuid': str(payment.uuid),
            'status': payment.status,
            'supplier': {
                'id': payment.supplier_id,
                'name': payment.supplier.name,
            },
            'principal_uzs': uzs_int(payment.principal_uzs),
            'fee_uzs': uzs_int(payment.fee_uzs),
            'total_debited_uzs': uzs_int(payment.total_debited_uzs),
            'source_account': payment.source_account or None,
            'supplier_balance_before_uzs': uzs_int(
                payment.supplier_balance_before_uzs
            ),
            'supplier_balance_after_uzs': uzs_int(
                payment.supplier_balance_after_uzs
            ),
            'source_balance_before_uzs': (
                uzs_int(payment.source_balance_before_uzs)
                if payment.source_balance_before_uzs is not None else None
            ),
            'source_balance_after_uzs': (
                uzs_int(payment.source_balance_after_uzs)
                if payment.source_balance_after_uzs is not None else None
            ),
            'supplier_transaction_id': payment.supplier_transaction_id,
            'treasury_transaction_id': payment.treasury_transaction_id,
            'allocations': allocations,
            'paid_at': local_iso(payment.paid_at),
            'action_history': actions,
        }
