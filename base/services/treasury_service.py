"""Branch-scoped SAFE/BANK balances backed by an append-only ledger."""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import TreasuryAccount, TreasuryTransaction
from base.money import (
    MoneyValueError,
    local_iso,
    signed_whole_uzs,
    uzs_int,
    whole_uzs,
)
from base.services.branch_scope import resolve_actor_branch


CENTS = Decimal('0.01')


class TreasurySettlementError(RuntimeError):
    def __init__(self, code, message, *, status=409, errors=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.errors = errors or {}
        self.details = details or {}

    def response(self):
        return ServiceResponse.failure(
            self.code,
            self.message,
            self.status,
            errors=self.errors,
            details=self.details,
        )


def _to_decimal(value):
    try:
        amount = Decimal(str(value)).quantize(CENTS)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _actor_name(actor):
    if actor is None:
        return ''
    return f'{actor.first_name} {actor.last_name}'.strip()


def _branch(branch_id=None, actor=None):
    branch_id = str(branch_id or resolve_actor_branch(actor) or '').strip()
    if not branch_id:
        node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if node_branch.lower() != 'cloud':
            branch_id = node_branch
    return branch_id


def _ensure_account(kind, branch_id):
    account = TreasuryAccount.objects.filter(
        kind=kind,
        branch_id=branch_id,
        is_deleted=False,
    ).first()
    if account is not None:
        return account
    try:
        with transaction.atomic():
            return TreasuryAccount.objects.create(
                kind=kind,
                branch_id=branch_id,
                balance=Decimal('0'),
            )
    except IntegrityError:
        return TreasuryAccount.objects.get(
            kind=kind,
            branch_id=branch_id,
            is_deleted=False,
        )


def _lock_accounts(kinds, branch_id):
    branch_id = _branch(branch_id)
    if not branch_id:
        raise ValueError('branch_id is required for Treasury')
    requested = sorted(set(kinds))
    for kind in requested:
        _ensure_account(kind, branch_id)
    rows = list(
        TreasuryAccount.objects.select_for_update()
        .filter(branch_id=branch_id, kind__in=requested, is_deleted=False)
        .order_by('pk')
    )
    accounts = {row.kind: row for row in rows}
    if set(accounts) != set(requested):
        raise RuntimeError('Treasury account initialization failed')
    return accounts


def _get_account_locked(kind, branch_id=None):
    return _lock_accounts([kind], _branch(branch_id))[kind]


def _apply(
    account,
    delta,
    transaction_type,
    *,
    fee=Decimal('0'),
    counterparty=None,
    category='',
    canonical_category=None,
    description='',
    reference_type='',
    reference_id=None,
    performed_by=None,
    branch_id=None,
    command_id=None,
    idempotency_key='',
    reversal_of=None,
):
    branch_id = _branch(branch_id, performed_by) or account.branch_id
    if account.branch_id != branch_id:
        raise ValueError('Treasury account belongs to another branch')
    before = account.balance or Decimal('0')
    after = before + delta
    account.balance = after
    account.last_updated = timezone.now()
    account.save(update_fields=['balance', 'last_updated'])
    category_code = getattr(canonical_category, 'code', '') or ''
    category_name = getattr(canonical_category, 'name', '') or ''
    return TreasuryTransaction.objects.create(
        account=account,
        type=transaction_type,
        delta=delta,
        fee=fee,
        balance_before=before,
        balance_after=after,
        counterparty=counterparty,
        category=category_code or category or '',
        canonical_category=canonical_category,
        category_code_snapshot=category_code,
        category_name_snapshot=category_name or category or '',
        description=description or '',
        reference_type=reference_type or '',
        reference_id=reference_id,
        performed_by=performed_by,
        actor_display_snapshot=_actor_name(performed_by),
        branch_id=branch_id,
        command_id=command_id,
        idempotency_key=idempotency_key or '',
        reversal_of=reversal_of,
    )


def _serialize_account(account):
    return {
        'kind': account.kind,
        'branch_id': account.branch_id,
        'balance': str(account.balance),
        'balance_uzs': uzs_int(account.balance),
        'last_updated': (
            local_iso(account.last_updated)
        ),
    }


def _serialize_transaction(row):
    actor_name = row.actor_display_snapshot
    if not actor_name and row.performed_by:
        actor_name = _actor_name(row.performed_by)
    return {
        'id': row.id,
        'account': row.account.kind if row.account else None,
        'type': row.type,
        'delta': str(row.delta),
        'delta_uzs': uzs_int(row.delta),
        'fee': str(row.fee),
        'fee_uzs': uzs_int(row.fee),
        'balance_before': str(row.balance_before),
        'balance_after': str(row.balance_after),
        'counterparty': row.counterparty.kind if row.counterparty else None,
        'category': row.category_name_snapshot or row.category,
        'category_id': row.canonical_category_id,
        'category_code': row.category_code_snapshot or row.category,
        'description': row.description,
        'reference_type': row.reference_type,
        'reference_id': row.reference_id,
        'performed_by_id': row.performed_by_id,
        'performed_by': actor_name or None,
        'reversal_of_id': row.reversal_of_id,
        'created_at': local_iso(row.created_at),
    }


def _tender_destinations(methods):
    from base.models import PaymentMethodConfig

    fixed = {
        'CASH': TreasuryAccount.Kind.SAFE,
        'CARD': TreasuryAccount.Kind.BANK,
        'UZCARD': TreasuryAccount.Kind.BANK,
        'HUMO': TreasuryAccount.Kind.BANK,
        'PAYME': TreasuryAccount.Kind.BANK,
    }
    destinations = {}
    configurable = []
    for method in methods:
        if method in fixed:
            destinations[method] = fixed[method]
        else:
            configurable.append(method)
    if configurable:
        rows = PaymentMethodConfig.objects.filter(
            is_active=True,
            treasury_destination=PaymentMethodConfig.TreasuryDestination.BANK,
        ).values_list('code', 'treasury_destination')
        configured = {
            str(code).strip().upper(): destination
            for code, destination in rows
        }
        destinations.update({
            method: configured[method]
            for method in configurable
            if method in configured
        })
    missing = sorted(set(methods) - set(destinations))
    if missing:
        raise TreasurySettlementError(
            'SETTLEMENT_TENDER_UNCLASSIFIED',
            'A tender method has no Treasury destination.',
            errors={'confirmed': [f'Unclassified tender(s): {", ".join(missing)}.']},
            details={'methods': missing},
        )
    return destinations


def _settlement_manifest(shift_id, branch_id, normalized, destinations):
    return {
        'version': 1,
        'shift_id': shift_id,
        'branch_id': branch_id,
        'tenders': [
            {
                'method': method,
                'destination': destinations[method],
                'amount_uzs': uzs_int(normalized[method]),
            }
            for method in sorted(normalized)
        ],
    }


class TreasuryService:
    @staticmethod
    @transaction.atomic
    def get_accounts(branch_id=None, actor=None):
        branch_id = _branch(branch_id, actor)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Treasury branch could not be resolved.', 403,
            )
        accounts = _lock_accounts(
            [TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK],
            branch_id,
        )
        return ServiceResponse.success(data={
            'accounts': {
                kind: _serialize_account(accounts[kind])
                for kind in (TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK)
            },
        })

    @staticmethod
    @transaction.atomic
    def post_shift_settlement(shift_id, tenders, performed_by=None, branch_id=None,
                              command_id=None, idempotency_key=''):
        from base.models import CashReconciliation, Shift

        branch_id = _branch(branch_id, performed_by)
        if not shift_id or not branch_id:
            raise TreasurySettlementError(
                'SETTLEMENT_SCOPE_INVALID',
                'Shift and branch are required for Treasury settlement.',
                status=422,
            )
        if not isinstance(tenders, dict):
            raise TreasurySettlementError(
                'SETTLEMENT_PAYLOAD_INVALID',
                'Tenders must be an object keyed by payment method.',
                status=422,
            )

        normalized = {}
        for raw_method, raw_amount in tenders.items():
            method = str(raw_method or '').strip().upper()
            if not method or len(method) > 50:
                raise TreasurySettlementError(
                    'SETTLEMENT_PAYLOAD_INVALID',
                    'Settlement contains an invalid tender method.',
                    status=422,
                )
            try:
                normalized[method] = signed_whole_uzs(
                    raw_amount,
                    f'confirmed.{method}',
                    maximum=Decimal('999999999999'),
                )
            except MoneyValueError as exc:
                raise TreasurySettlementError(
                    'SETTLEMENT_PAYLOAD_INVALID',
                    'Settlement contains an invalid amount.',
                    status=422,
                    errors={f'confirmed.{method}': [str(exc)]},
                ) from exc

        shift = Shift.objects.select_for_update().filter(
            pk=shift_id,
            branch_id=branch_id,
            is_deleted=False,
        ).first()
        if shift is None:
            raise TreasurySettlementError(
                'SETTLEMENT_SHIFT_NOT_FOUND',
                'Shift was not found in the authorized branch.',
                status=404,
            )
        reconciliation = CashReconciliation.objects.select_for_update().filter(
            shift_id=shift_id,
            is_deleted=False,
        ).first()
        if reconciliation is None:
            raise TreasurySettlementError(
                'SETTLEMENT_RECONCILIATION_REQUIRED',
                'Manager reconciliation is required before Treasury posting.',
                status=409,
                details={'shift_id': shift_id},
            )
        destinations = _tender_destinations(normalized)
        manifest = _settlement_manifest(
            shift_id,
            branch_id,
            normalized,
            destinations,
        )
        if reconciliation and reconciliation.treasury_posting_manifest:
            if reconciliation.treasury_posting_manifest != manifest:
                raise TreasurySettlementError(
                    'SETTLEMENT_POSTING_CONFLICT',
                    'This shift already has a different Treasury posting.',
                    details={'shift_id': shift_id},
                )

        accounts = _lock_accounts(destinations.values(), branch_id)
        existing_rows = {
            row.category: row
            for row in TreasuryTransaction.objects.select_for_update()
            .filter(
                type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
                reference_type='ShiftSettlement',
                reference_id=shift_id,
            )
            .select_related('account')
            .order_by('pk')
        }
        postings = []
        for method in sorted(normalized):
            amount = normalized[method]
            destination = destinations[method]
            existing = existing_rows.get(method)
            if existing is not None:
                if (
                    existing.account.kind != destination
                    or existing.account.branch_id != branch_id
                    or existing.branch_id != branch_id
                    or existing.delta != amount
                ):
                    raise TreasurySettlementError(
                        'SETTLEMENT_POSTING_CONFLICT',
                        'This tender already has a different Treasury posting.',
                        details={'shift_id': shift_id, 'method': method},
                    )
                transaction_row = existing
            elif amount == 0:
                transaction_row = None
            else:
                transaction_row = _apply(
                    accounts[destination],
                    amount,
                    TreasuryTransaction.Type.SHIFT_DEPOSIT,
                    category=method,
                    description=(
                        f'Shift {shift_id} manager settlement: {method}'
                        if amount > 0
                        else f'Shift {shift_id} refund reversal: {method}'
                    ),
                    reference_type='ShiftSettlement',
                    reference_id=shift_id,
                    performed_by=performed_by,
                    branch_id=branch_id,
                    command_id=command_id,
                    idempotency_key=idempotency_key,
                )
            postings.append({
                'method': method,
                'destination': destination,
                'amount_uzs': uzs_int(amount),
                'treasury_transaction_id': (
                    transaction_row.id if transaction_row else None
                ),
            })

        posted_at = timezone.now()
        if reconciliation:
            reconciliation.treasury_posting_manifest = manifest
            if reconciliation.treasury_posted_at is None:
                reconciliation.treasury_posted_at = posted_at
            else:
                posted_at = reconciliation.treasury_posted_at
            reconciliation.save(update_fields=[
                'treasury_posting_manifest',
                'treasury_posted_at',
            ])
        elif existing_rows:
            posted_at = min(row.created_at for row in existing_rows.values())

        total = sum(normalized.values(), Decimal('0'))
        unique_destinations = {
            destinations[method]
            for method, amount in normalized.items()
            if amount != 0
        }
        return {
            'status': 'POSTED',
            'shift_id': shift_id,
            'branch_id': branch_id,
            'total_uzs': uzs_int(total),
            'postings': postings,
            'posted_at': local_iso(posted_at),
            'total': str(total.quantize(CENTS)),
            'account': (
                next(iter(unique_destinations))
                if len(unique_destinations) == 1 else None
            ),
            'tenders': [
                {'method': row['method'], 'amount': str(normalized[row['method']])}
                for row in postings
            ],
            'entry_ids': [
                row['treasury_transaction_id']
                for row in postings
                if row['treasury_transaction_id'] is not None
            ],
        }

    @staticmethod
    @transaction.atomic
    def plan_inkassa_allocation(branch_id, method_amounts):
        branch_id = _branch(branch_id)
        if not branch_id:
            raise ValueError('branch_id is required for Inkassa allocation')
        if not isinstance(method_amounts, dict) or not method_amounts:
            raise ValueError('method_amounts are required for Inkassa allocation')

        methods = [str(method).strip().upper() for method in method_amounts]
        destinations = _tender_destinations(methods)
        _lock_accounts(destinations.values(), branch_id)

        from base.models import Inkassa

        refund_prefix = Inkassa.refund_command_prefix()
        plans = {}
        for method, amount in sorted(method_amounts.items()):
            method = str(method).strip().upper()
            recognized_total = (
                TreasuryTransaction.objects.filter(
                    type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
                    reference_type='ShiftSettlement',
                    category=method,
                    branch_id=branch_id,
                ).aggregate(total=Sum('delta'))['total']
                or Decimal('0.00')
            )
            recognized_consumed = (
                Inkassa.objects.filter(
                    branch_id=branch_id,
                    inkass_type=method,
                    treasury_allocated_at__isnull=False,
                ).exclude(notes__startswith=refund_prefix)
                .aggregate(total=Sum('settlement_offset_amount'))['total']
                or Decimal('0.00')
            )
            recognized_net = recognized_total - recognized_consumed
            recognized_available = max(recognized_net, Decimal('0.00'))
            matched = min(amount, recognized_available)
            remainder = amount - matched
            legacy = remainder if method == 'CASH' else Decimal('0.00')
            plans[method] = {
                'collected': amount,
                'matched_recognized': matched,
                'legacy_opening': legacy,
                'safe_delta': legacy,
                'unallocated': (
                    Decimal('0.00') if method == 'CASH' else remainder
                ),
                'recognized_net': recognized_net,
            }
        return plans

    @staticmethod
    @transaction.atomic
    def post_legacy_inkassa(
        inkassa_id,
        amount,
        method,
        *,
        branch_id,
        performed_by=None,
    ):
        amount = _to_decimal(amount)
        if amount is None or amount < 0:
            raise ValueError('invalid legacy Inkassa amount')
        if amount == 0:
            return None
        branch_id = _branch(branch_id, performed_by)
        method = str(method or '').strip().upper()
        if not branch_id or not method:
            raise ValueError('branch_id and method are required')
        safe = _get_account_locked(TreasuryAccount.Kind.SAFE, branch_id)
        existing = TreasuryTransaction.objects.select_for_update().filter(
            type=TreasuryTransaction.Type.INKASSA,
            reference_type='InkassaLegacy',
            reference_id=inkassa_id,
        ).select_related('account').first()
        if existing is not None:
            if (
                existing.account_id != safe.id
                or existing.branch_id != branch_id
                or existing.category != method
                or existing.delta != amount
            ):
                raise ValueError(
                    f'conflicting legacy Inkassa posting for row {inkassa_id}'
                )
            return existing
        return _apply(
            safe,
            amount,
            TreasuryTransaction.Type.INKASSA,
            category=method,
            description=f'Approved legacy opening via Inkassa {inkassa_id}: {method}',
            reference_type='InkassaLegacy',
            reference_id=inkassa_id,
            performed_by=performed_by,
            branch_id=branch_id,
        )

    @staticmethod
    @transaction.atomic
    def transfer(
        from_kind,
        to_kind,
        amount,
        fee=0,
        performed_by=None,
        description='',
        *,
        actor=None,
        branch_id=None,
        command_id=None,
        idempotency_key='',
    ):
        from_kind = str(from_kind or '').upper()
        to_kind = str(to_kind or '').upper()
        valid = {TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK}
        if from_kind not in valid or to_kind not in valid or from_kind == to_kind:
            return ServiceResponse.failure(
                'TREASURY_TRANSFER_ACCOUNTS_INVALID',
                'Transfer accounts must be different SAFE/BANK accounts.',
                422,
                errors={'account': ['from and to must be different SAFE/BANK accounts.']},
            )
        try:
            amount = whole_uzs(
                amount, 'amount_uzs', positive=True,
                maximum=Decimal('999999999999'),
            )
            fee = whole_uzs(
                fee, 'fee_uzs', maximum=Decimal('999999999999'),
            )
        except MoneyValueError as exc:
            return ServiceResponse.failure(
                'TREASURY_TRANSFER_AMOUNT_INVALID',
                'Transfer amount is invalid.',
                422,
                errors={'amount_uzs': [str(exc)]},
            )
        if fee > amount:
            return ServiceResponse.failure(
                'TREASURY_TRANSFER_FEE_INVALID',
                'Transfer fee cannot exceed the amount.',
                422,
                errors={'fee_uzs': ['Fee cannot exceed amount_uzs.']},
            )
        branch_id = _branch(branch_id, performed_by)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Treasury branch could not be resolved.', 403,
            )

        if command_id:
            existing = list(
                TreasuryTransaction.objects.filter(
                    command_id=command_id,
                    branch_id=branch_id,
                    type__in=[
                        TreasuryTransaction.Type.TRANSFER_OUT,
                        TreasuryTransaction.Type.TRANSFER_IN,
                    ],
                ).select_related('account', 'counterparty', 'performed_by')
                .order_by('pk')
            )
            if len(existing) == 2:
                accounts = {row.account.kind: row.account for row in existing}
                return ServiceResponse.success(data={
                    'amount': str(amount),
                    'fee': str(fee),
                    'credited': str(amount - fee),
                    'from': _serialize_account(accounts[from_kind]),
                    'to': _serialize_account(accounts[to_kind]),
                    'transactions': [
                        _serialize_transaction(row) for row in existing
                    ],
                }, message='Transfer already completed')

        accounts = _lock_accounts([from_kind, to_kind], branch_id)
        source = accounts[from_kind]
        destination = accounts[to_kind]
        if command_id:
            existing = list(
                TreasuryTransaction.objects.filter(
                    command_id=command_id,
                    branch_id=branch_id,
                    type__in=[
                        TreasuryTransaction.Type.TRANSFER_OUT,
                        TreasuryTransaction.Type.TRANSFER_IN,
                    ],
                ).select_related('account', 'counterparty', 'performed_by')
                .order_by('pk')
            )
            if len(existing) == 2:
                return ServiceResponse.success(data={
                    'amount': str(amount),
                    'fee': str(fee),
                    'credited': str(amount - fee),
                    'from': _serialize_account(source),
                    'to': _serialize_account(destination),
                    'transactions': [
                        _serialize_transaction(row) for row in existing
                    ],
                }, message='Transfer already completed')
            if existing:
                return ServiceResponse.conflict(
                    'TREASURY_TRANSFER_INCOMPLETE',
                    'The transfer has incomplete ledger evidence.',
                    details={'transaction_ids': [row.id for row in existing]},
                )
        if (source.balance or Decimal('0')) < amount:
            return ServiceResponse.failure(
                'INSUFFICIENT_FUNDS',
                'The source account has insufficient funds.',
                422,
                errors={'amount_uzs': ['Available balance is below the required debit.']},
                details={
                    'available_uzs': uzs_int(source.balance),
                    'required_uzs': uzs_int(amount),
                },
            )
        outgoing = _apply(
            source,
            -amount,
            TreasuryTransaction.Type.TRANSFER_OUT,
            fee=fee,
            counterparty=destination,
            description=description or f'Transfer to {to_kind}',
            performed_by=performed_by,
            branch_id=branch_id,
            command_id=command_id,
            idempotency_key=idempotency_key,
        )
        credited = amount - fee
        incoming = _apply(
            destination,
            credited,
            TreasuryTransaction.Type.TRANSFER_IN,
            fee=fee,
            counterparty=source,
            description=description or f'Transfer from {from_kind}',
            performed_by=performed_by,
            branch_id=branch_id,
            command_id=command_id,
            idempotency_key=idempotency_key,
        )
        return ServiceResponse.success(data={
            'amount': str(amount),
            'fee': str(fee),
            'credited': str(credited),
            'from': _serialize_account(source),
            'to': _serialize_account(destination),
            'transactions': [
                _serialize_transaction(outgoing),
                _serialize_transaction(incoming),
            ],
        }, message='Transfer completed')

    @staticmethod
    @transaction.atomic
    def record_expense(
        account_kind,
        amount,
        category='',
        description='',
        performed_by=None,
        fee=0,
        txn_type=None,
        reference_type='',
        reference_id=None,
        *,
        canonical_category=None,
        branch_id=None,
        command_id=None,
        idempotency_key='',
    ):
        account_kind = str(account_kind or '').upper()
        if account_kind not in {
            TreasuryAccount.Kind.SAFE,
            TreasuryAccount.Kind.BANK,
        }:
            return ServiceResponse.failure(
                'TREASURY_ACCOUNT_INVALID',
                'Source account must be SAFE or BANK.',
                422,
                errors={'source_account': ['Must be SAFE or BANK.']},
            )
        try:
            amount = whole_uzs(
                amount, 'amount_uzs', positive=True,
                maximum=Decimal('999999999999'),
            )
            fee = whole_uzs(
                fee, 'fee_uzs', maximum=Decimal('999999999999'),
            )
        except MoneyValueError as exc:
            return ServiceResponse.failure(
                'EXPENSE_AMOUNT_INVALID',
                'Expense amount is invalid.',
                422,
                errors={'amount_uzs': [str(exc)]},
            )
        if account_kind != TreasuryAccount.Kind.BANK and fee:
            return ServiceResponse.failure(
                'FEE_BANK_ONLY',
                'A fee is allowed only for BANK payments.',
                422,
                errors={'fee_uzs': ['Fee must be zero for SAFE.']},
            )
        branch_id = _branch(branch_id, performed_by)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Treasury branch could not be resolved.', 403,
            )
        transaction_type = txn_type or TreasuryTransaction.Type.EXPENSE
        if command_id:
            existing = TreasuryTransaction.objects.filter(
                command_id=command_id,
                branch_id=branch_id,
                type=transaction_type,
            ).select_related(
                'account', 'counterparty', 'performed_by', 'canonical_category',
            ).first()
            if existing:
                return ServiceResponse.success(data={
                    'account': _serialize_account(existing.account),
                    'fee': str(existing.fee),
                    'transaction': _serialize_transaction(existing),
                }, message='Expense already recorded')

        account = _get_account_locked(account_kind, branch_id)
        if command_id:
            existing = TreasuryTransaction.objects.filter(
                command_id=command_id,
                branch_id=branch_id,
                type=transaction_type,
            ).select_related(
                'account', 'counterparty', 'performed_by', 'canonical_category',
            ).first()
            if existing:
                return ServiceResponse.success(data={
                    'account': _serialize_account(existing.account),
                    'fee': str(existing.fee),
                    'transaction': _serialize_transaction(existing),
                }, message='Expense already recorded')
        total = amount + fee
        if total > Decimal('999999999999'):
            return ServiceResponse.validation_error({
                'amount_uzs': ['Amount plus fee exceeds the Treasury model limit.'],
            })
        if (account.balance or Decimal('0')) < total:
            return ServiceResponse.failure(
                'INSUFFICIENT_FUNDS',
                'The source account has insufficient funds.',
                422,
                errors={'amount_uzs': ['Available balance is below the required debit.']},
                details={
                    'available_uzs': uzs_int(account.balance),
                    'required_uzs': uzs_int(total),
                },
            )
        row = _apply(
            account,
            -total,
            transaction_type,
            category=category,
            canonical_category=canonical_category,
            description=description,
            fee=fee,
            reference_type=reference_type,
            reference_id=reference_id,
            performed_by=performed_by,
            branch_id=branch_id,
            command_id=command_id,
            idempotency_key=idempotency_key,
        )
        return ServiceResponse.created(data={
            'account': _serialize_account(account),
            'fee': str(fee),
            'transaction': _serialize_transaction(row),
        }, message='Expense recorded')

    @staticmethod
    @transaction.atomic
    def reverse_transaction(
        transaction_id,
        *,
        performed_by,
        reason,
        branch_id=None,
        command_id=None,
        transaction_type=TreasuryTransaction.Type.EXPENSE_REVERSAL,
        idempotency_key='',
    ):
        branch_id = _branch(branch_id, performed_by)
        original = TreasuryTransaction.objects.select_for_update(of=('self',)).select_related(
            'account', 'canonical_category',
        ).filter(
            pk=transaction_id,
            branch_id=branch_id,
        ).first()
        if original is None:
            return ServiceResponse.not_found('Treasury transaction not found')
        reversal = TreasuryTransaction.objects.filter(
            reversal_of=original,
        ).select_related('account', 'performed_by', 'canonical_category').first()
        if reversal:
            return ServiceResponse.success(data={
                'transaction': _serialize_transaction(reversal),
                'account': _serialize_account(reversal.account),
            }, message='Transaction already reversed')
        account = _get_account_locked(original.account.kind, branch_id)
        reversal = _apply(
            account,
            -original.delta,
            transaction_type,
            fee=-original.fee,
            canonical_category=original.canonical_category,
            category=original.category,
            description=reason,
            reference_type=original.reference_type,
            reference_id=original.reference_id,
            performed_by=performed_by,
            branch_id=branch_id,
            command_id=command_id,
            idempotency_key=idempotency_key,
            reversal_of=original,
        )
        return ServiceResponse.created(data={
            'transaction': _serialize_transaction(reversal),
            'account': _serialize_account(account),
        }, message='Transaction reversed')

    @staticmethod
    def history(
        account_kind=None,
        txn_type=None,
        page=1,
        per_page=20,
        *,
        branch_id=None,
        date_from=None,
        date_to=None,
        category_id=None,
        reference_type=None,
        reference_id=None,
        performed_by_id=None,
        search=None,
        actor=None,
    ):
        branch_id = _branch(branch_id, actor)
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Treasury branch could not be resolved.', 403,
            )
        queryset = TreasuryTransaction.objects.filter(
            is_deleted=False,
            branch_id=branch_id,
        ).select_related(
            'account', 'counterparty', 'performed_by', 'canonical_category',
        )
        if account_kind:
            account_kind = str(account_kind).upper()
            if account_kind not in TreasuryAccount.Kind.values:
                return ServiceResponse.validation_error({
                    'account': ['Must be SAFE or BANK.'],
                })
            queryset = queryset.filter(account__kind=account_kind)
        if txn_type:
            txn_type = str(txn_type).upper()
            if txn_type not in TreasuryTransaction.Type.values:
                return ServiceResponse.validation_error({
                    'type': ['Unknown Treasury transaction type.'],
                })
            queryset = queryset.filter(type=txn_type)
        if date_from:
            queryset = queryset.filter(created_at__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__lt=date_to)
        if category_id is not None:
            queryset = queryset.filter(canonical_category_id=category_id)
        if reference_type:
            queryset = queryset.filter(reference_type=reference_type)
        if reference_id is not None:
            queryset = queryset.filter(reference_id=reference_id)
        if performed_by_id is not None:
            queryset = queryset.filter(performed_by_id=performed_by_id)
        if search:
            term = str(search).strip()
            query = (
                Q(description__icontains=term)
                | Q(category__icontains=term)
                | Q(category_name_snapshot__icontains=term)
                | Q(reference_type__icontains=term)
                | Q(actor_display_snapshot__icontains=term)
                | Q(performed_by__first_name__icontains=term)
                | Q(performed_by__last_name__icontains=term)
            )
            if term.isdigit():
                query |= Q(reference_id=int(term))
            queryset = queryset.filter(query)

        totals = queryset.aggregate(
            inflow=Sum('delta', filter=Q(delta__gt=0)),
            outflow=Sum('delta', filter=Q(delta__lt=0)),
            fee=Sum('fee'),
        )
        total = queryset.count()
        rows = queryset.order_by('-created_at', '-id')[
            (page - 1) * per_page:page * per_page
        ]
        total_pages = (total + per_page - 1) // per_page
        return ServiceResponse.success(data={
            'transactions': [_serialize_transaction(row) for row in rows],
            'totals': {
                'total_inflow_uzs': uzs_int(totals['inflow'] or 0),
                'total_outflow_uzs': uzs_int(-(totals['outflow'] or 0)),
                'total_fee_uzs': uzs_int(totals['fee'] or 0),
                'row_count': total,
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': total_pages,
                'pages': total_pages,
            },
        })
