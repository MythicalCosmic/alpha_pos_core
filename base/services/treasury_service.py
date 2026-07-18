"""SAFE + BANK treasury: balances, shift settlement, transfers, expenses.

Two accounts sit above the till drawer (CashRegister):
  * SAFE — every tender accepted by a manager at shift reconciliation.
  * BANK — explicit bank-side transfers and spending outside shift handover.

Shift reconciliation is the sole recognition boundary for shift proceeds.
Inkassa may still remove physical cash from a branch register, but that later
movement is audit/transport only and must never recognize the sale again.

Every balance change is row-locked and written to the TreasuryTransaction
ledger (append-only) so the books always reconcile.

Transfer fee convention: a transfer moves `amount` out of the source account
and credits `amount - fee` to the destination; `fee` is the processor/bank
charge (recorded as a FEE ledger row). e.g. withdraw 1,000,000 from BANK with
a 5,000 fee → BANK -1,000,000, SAFE +995,000, fee 5,000.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction, IntegrityError
from django.db.models import F, Sum
from django.utils import timezone

from base.models import TreasuryAccount, TreasuryTransaction
from base.helpers.response import ServiceResponse

CENTS = Decimal('0.01')


def _to_decimal(value):
    try:
        return Decimal(str(value)).quantize(CENTS)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _get_account_locked(kind):
    """Row-locked account for the given kind, created at zero if missing.

    Must run inside an atomic block (callers are decorated).

    The database enforces one active row per kind. Catch the losing INSERT in a
    first-use race and re-fetch the winner with its row lock held."""
    acct = (
        TreasuryAccount.objects.select_for_update()
        .filter(kind=kind, is_deleted=False)
        .first()
    )
    if acct:
        return acct
    try:
        with transaction.atomic():
            acct = TreasuryAccount.objects.create(kind=kind, balance=Decimal('0'))
    except IntegrityError:
        # Lost an INSERT race (only possible once the unique constraint above
        # exists) — the winner's row is now committed; fall through to re-fetch.
        acct = None
    if acct is None:
        acct = (
            TreasuryAccount.objects.select_for_update()
            .filter(kind=kind, is_deleted=False)
            .first()
        )
    else:
        acct = TreasuryAccount.objects.select_for_update().get(pk=acct.pk)
    return acct


def _apply(acct, delta, txn_type, *, fee=Decimal('0'), counterparty=None,
           category='', description='', reference_type='', reference_id=None,
           performed_by=None, branch_id=None):
    """Mutate a locked account by `delta` and write the ledger row."""
    before = acct.balance or Decimal('0')
    after = before + delta
    acct.balance = after
    acct.last_updated = timezone.now()
    acct.save(update_fields=['balance', 'last_updated', 'synced_at', 'sync_version'])
    return TreasuryTransaction.objects.create(
        account=acct,
        type=txn_type,
        delta=delta,
        fee=fee,
        balance_before=before,
        balance_after=after,
        counterparty=counterparty,
        category=category or '',
        description=description or '',
        reference_type=reference_type or '',
        reference_id=reference_id,
        performed_by=performed_by,
        branch_id=branch_id or acct.branch_id,
    )


def _serialize_account(acct):
    return {
        'kind': acct.kind,
        'balance': str(acct.balance),
        'last_updated': acct.last_updated.isoformat() if acct.last_updated else None,
    }


def _serialize_txn(t):
    return {
        'id': t.id,
        'account': t.account.kind if t.account else None,
        'type': t.type,
        'delta': str(t.delta),
        'fee': str(t.fee),
        'balance_before': str(t.balance_before),
        'balance_after': str(t.balance_after),
        'counterparty': t.counterparty.kind if t.counterparty else None,
        'category': t.category,
        'description': t.description,
        'reference_type': t.reference_type,
        'reference_id': t.reference_id,
        'performed_by': (
            f"{t.performed_by.first_name} {t.performed_by.last_name}".strip()
            if t.performed_by else None
        ),
        'created_at': t.created_at.isoformat() if t.created_at else None,
    }


class TreasuryService:

    @staticmethod
    def get_accounts():
        # The conditional unique constraint makes concurrent first-use
        # get_or_create calls converge on one active account per kind.
        data = {}
        for kind in (TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK):
            acct, _ = TreasuryAccount.objects.get_or_create(
                kind=kind, is_deleted=False, defaults={'balance': Decimal('0')},
            )
            data[kind] = _serialize_account(acct)
        return ServiceResponse.success(data={'accounts': data})

    @staticmethod
    @transaction.atomic
    def deposit_inkassa(cash_amount, card_amount, performed_by=None, reference_id=None):
        """Deprecated no-op retained for binary/API compatibility.

        Inkassa is now physical register movement/audit only. Shift proceeds
        are recognized exactly once by ``post_shift_settlement`` when the
        manager reconciles them, so this legacy helper must never mutate SAFE
        or BANK even if an older integration still calls it.
        """
        return None, None

    @staticmethod
    @transaction.atomic
    def deposit_shift(cash_amount, card_amount, performed_by=None, reference_id=None):
        """Deprecated no-op; use ``post_shift_settlement``.

        The old cash/card signature cannot preserve a dynamic tender breakdown
        and was not idempotent. Leaving it capable of changing balances would
        create a second recognition path, so it remains callable only as a safe
        compatibility shim.
        """
        return None, None

    @staticmethod
    @transaction.atomic
    def post_shift_settlement(shift_id, tenders, performed_by=None,
                              branch_id=None):
        """Idempotently recognize one manager-confirmed shift into SAFE.

        A separate append-only ``SHIFT_DEPOSIT`` row is written for each
        positive tender. ``(shift_id, method)`` is protected both by the SAFE
        account row lock and by a conditional database uniqueness constraint,
        so an HTTP retry, concurrent request, or future alternate writer cannot
        credit the same shift tender twice.

        Zero tenders remain in the response for a complete audit payload but do
        not create ledger noise. Signed negative amounts are deliberate refund
        reversals and debit SAFE under the same shift+tender identity. A
        persisted row whose amount differs from the requested manager
        confirmation is a hard accounting conflict; history is never silently
        rewritten.
        """
        if not shift_id:
            raise ValueError('shift_id is required for treasury settlement')
        branch_id = str(branch_id or '').strip()
        if not branch_id:
            raise ValueError('branch_id is required for treasury settlement')
        if not isinstance(tenders, dict):
            raise ValueError('tenders must be an object keyed by payment method')

        normalized = {}
        for raw_method, raw_amount in tenders.items():
            method = str(raw_method or '').strip().upper()
            if not method or len(method) > 50:
                raise ValueError('invalid settlement payment method')
            amount = _to_decimal(raw_amount)
            if amount is None or not amount.is_finite():
                raise ValueError(f'invalid settlement amount for {method}')
            normalized[method] = amount

        movements = {
            method: amount for method, amount in normalized.items()
            if amount != 0
        }
        entries = []
        if movements:
            # This lock serializes every balance mutation. Query idempotency
            # rows only after it is held so a concurrent retry sees the first
            # transaction once that writer releases the account.
            safe = _get_account_locked(TreasuryAccount.Kind.SAFE)
            for method in sorted(movements):
                amount = movements[method]
                existing = (
                    TreasuryTransaction.objects.select_for_update()
                    .filter(
                        type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
                        reference_type='ShiftSettlement',
                        reference_id=shift_id,
                        category=method,
                    )
                    .select_related('account')
                    .first()
                )
                if existing is not None:
                    if (
                        existing.account_id != safe.id
                        or existing.account.kind != TreasuryAccount.Kind.SAFE
                        or existing.branch_id != branch_id
                        or existing.delta != amount
                    ):
                        raise ValueError(
                            f'conflicting treasury posting for shift '
                            f'{shift_id} tender {method}'
                        )
                    entries.append(existing)
                    continue

                entries.append(_apply(
                    safe,
                    amount,
                    TreasuryTransaction.Type.SHIFT_DEPOSIT,
                    category=method,
                    description=(
                        f'Shift {shift_id} manager settlement: {method}'
                        if amount > 0 else
                        f'Shift {shift_id} refund reversal: {method}'
                    ),
                    reference_type='ShiftSettlement',
                    reference_id=shift_id,
                    performed_by=performed_by,
                    branch_id=branch_id,
                ))

        total = sum(normalized.values(), Decimal('0.00'))
        return {
            # A validated all-zero settlement is an atomically completed no-op,
            # not a pending posting. CashReconciliation.treasury_posted_at is
            # the durable marker when no ledger row is necessary.
            'status': 'posted',
            'account': TreasuryAccount.Kind.SAFE,
            'total': str(total.quantize(CENTS)),
            'tenders': [
                {'method': method, 'amount': str(amount.quantize(CENTS))}
                for method, amount in sorted(movements.items())
            ],
            'entry_ids': [entry.id for entry in entries],
        }

    @staticmethod
    @transaction.atomic
    def plan_inkassa_allocation(branch_id, method_amounts):
        """Freeze a branch collection plan under the shared SAFE lock.

        Reconciliation and this planner take the same account row lock before
        reading/writing settlement recognition. This prevents Inkassa from
        observing a stale pool while a manager confirmation commits.

        ``matched_recognized`` consumes already-posted ShiftSettlement value
        and therefore creates no second SAFE delta. Only unmatched physical
        CASH can become a separately approved legacy opening; its caller holds
        and caps against the branch CashRegister. Unmatched non-cash is always
        rejected until an immutable provider cutover snapshot/manual adjustment
        exists. Signed refund entries remain in the cumulative pool, so they
        must be repaid by later positive settlements before any value becomes
        matchable again.
        """
        branch_id = str(branch_id or '').strip()
        if not branch_id:
            raise ValueError('branch_id is required for Inkassa allocation')
        if not isinstance(method_amounts, dict) or not method_amounts:
            raise ValueError('method_amounts are required for Inkassa allocation')

        # Lock first, query second. All ShiftSettlement writers use this lock.
        _get_account_locked(TreasuryAccount.Kind.SAFE)

        from base.models import Inkassa

        refund_prefix = Inkassa.refund_command_prefix()
        plans = {}
        for method, amount in sorted(method_amounts.items()):
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
                ).exclude(
                    notes__startswith=refund_prefix,
                ).aggregate(total=Sum('settlement_offset_amount'))['total']
                or Decimal('0.00')
            )
            # Do not clamp before subtracting consumption: a signed refund may
            # make this negative, and later positive settlements must first
            # clear that debt before becoming available to match.
            recognized_net = recognized_total - recognized_consumed
            recognized_available = max(recognized_net, Decimal('0.00'))
            matched = min(amount, recognized_available)

            remainder = amount - matched
            legacy = remainder if method == 'CASH' else Decimal('0.00')
            unallocated = Decimal('0.00') if method == 'CASH' else remainder
            plans[method] = {
                'collected': amount,
                'matched_recognized': matched,
                'legacy_opening': legacy,
                'safe_delta': legacy,
                'unallocated': unallocated,
                'recognized_net': recognized_net,
            }
        return plans

    @staticmethod
    @transaction.atomic
    def post_legacy_inkassa(inkassa_id, amount, method, *, branch_id,
                             performed_by=None):
        """Idempotently post one evidence-bounded legacy opening to SAFE."""
        amount = _to_decimal(amount)
        if amount is None or not amount.is_finite() or amount < 0:
            raise ValueError('invalid legacy Inkassa amount')
        if amount == 0:
            return None
        branch_id = str(branch_id or '').strip()
        method = str(method or '').strip().upper()
        if not branch_id or not method:
            raise ValueError('branch_id and method are required')

        safe = _get_account_locked(TreasuryAccount.Kind.SAFE)
        existing = (
            TreasuryTransaction.objects.select_for_update()
            .filter(
                type=TreasuryTransaction.Type.INKASSA,
                reference_type='InkassaLegacy',
                reference_id=inkassa_id,
            )
            .select_related('account')
            .first()
        )
        if existing is not None:
            if (
                existing.account_id != safe.id
                or existing.account.kind != TreasuryAccount.Kind.SAFE
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
    def transfer(from_kind, to_kind, amount, fee=0, performed_by=None, description=''):
        """Move `amount` from one account to another, charging `fee`.

        Destination is credited `amount - fee`. Source must hold the full
        `amount`. Writes a TRANSFER_OUT (with fee) and a TRANSFER_IN row.
        """
        from_kind = (from_kind or '').upper()
        to_kind = (to_kind or '').upper()
        valid = {TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK}
        if from_kind not in valid or to_kind not in valid:
            return ServiceResponse.validation_error(
                errors={'account': 'from/to must be SAFE or BANK'},
                message='Invalid account')
        if from_kind == to_kind:
            return ServiceResponse.validation_error(
                errors={'account': 'from and to must differ'},
                message='Invalid transfer')

        amt = _to_decimal(amount)
        fee_d = _to_decimal(fee) or Decimal('0')
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than 0'},
                message='Invalid amount')
        if fee_d < 0:
            return ServiceResponse.validation_error(
                errors={'fee': 'Fee cannot be negative'}, message='Invalid fee')
        if fee_d > amt:
            return ServiceResponse.validation_error(
                errors={'fee': 'Fee cannot exceed the transfer amount'},
                message='Invalid fee')

        src = _get_account_locked(from_kind)
        if (src.balance or Decimal('0')) < amt:
            return ServiceResponse.validation_error(
                errors={'amount': f'{from_kind} balance {src.balance} is less than {amt}'},
                message='Insufficient funds')
        dst = _get_account_locked(to_kind)

        out_txn = _apply(
            src, -amt, TreasuryTransaction.Type.TRANSFER_OUT,
            fee=fee_d, counterparty=dst,
            description=description or f'Transfer to {to_kind}',
            performed_by=performed_by)
        credited = amt - fee_d
        in_txn = _apply(
            dst, credited, TreasuryTransaction.Type.TRANSFER_IN,
            fee=fee_d, counterparty=src,
            description=description or f'Transfer from {from_kind}',
            performed_by=performed_by)

        return ServiceResponse.success(
            data={
                'amount': str(amt), 'fee': str(fee_d), 'credited': str(credited),
                'from': _serialize_account(src), 'to': _serialize_account(dst),
                'transactions': [_serialize_txn(out_txn), _serialize_txn(in_txn)],
            },
            message='Transfer completed')

    @staticmethod
    @transaction.atomic
    def record_expense(account_kind, amount, category='', description='',
                       performed_by=None, fee=0, txn_type=None,
                       reference_type='', reference_id=None):
        """Spend money out of SAFE or BANK.

        `fee` is an optional commission (e.g. a bank charge on a BANK payment):
        the account is debited `amount + fee` and the fee is recorded on the
        ledger row, mirroring the transfer-fee convention.

        `txn_type` lets callers tag the ledger row (e.g. SUPPLIER_PAYMENT,
        SALARY_PAYMENT) with an optional reference_type/reference_id; it defaults
        to a plain EXPENSE.
        """
        account_kind = (account_kind or '').upper()
        if account_kind not in {TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK}:
            return ServiceResponse.validation_error(
                errors={'account': 'account must be SAFE or BANK'},
                message='Invalid account')
        amt = _to_decimal(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than 0'},
                message='Invalid amount')
        fee_d = _to_decimal(fee) or Decimal('0')
        if fee_d < 0:
            return ServiceResponse.validation_error(
                errors={'fee': 'Fee cannot be negative'}, message='Invalid fee')
        total = amt + fee_d

        acct = _get_account_locked(account_kind)
        if (acct.balance or Decimal('0')) < total:
            return ServiceResponse.validation_error(
                errors={'amount': f'{account_kind} balance {acct.balance} is less than {total}'},
                message='Insufficient funds')

        txn = _apply(
            acct, -total, txn_type or TreasuryTransaction.Type.EXPENSE,
            category=category or '', description=description or '',
            fee=fee_d, reference_type=reference_type or '',
            reference_id=reference_id, performed_by=performed_by)
        return ServiceResponse.created(
            data={'account': _serialize_account(acct), 'fee': str(fee_d),
                  'transaction': _serialize_txn(txn)},
            message='Expense recorded')

    @staticmethod
    def history(account_kind=None, txn_type=None, page=1, per_page=20):
        qs = TreasuryTransaction.objects.filter(is_deleted=False).select_related(
            'account', 'counterparty', 'performed_by')
        if account_kind:
            qs = qs.filter(account__kind=account_kind.upper())
        if txn_type:
            qs = qs.filter(type=txn_type.upper())
        total = qs.count()
        items = qs[(page - 1) * per_page: page * per_page]
        return ServiceResponse.success(data={
            'transactions': [_serialize_txn(t) for t in items],
            'pagination': {
                'page': page, 'per_page': per_page, 'total': total,
                'pages': (total + per_page - 1) // per_page,
            },
        })
