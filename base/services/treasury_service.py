"""SAFE + BANK treasury: balances, inkassa deposits, transfers, expenses.

Two accounts sit above the till drawer (CashRegister):
  * SAFE — physical cash moved out of the registers by inkassa.
  * BANK — electronic money (card / Payme), which never touches the drawer.

Every balance change is row-locked and written to the TreasuryTransaction
ledger (append-only) so the books always reconcile.

Transfer fee convention: a transfer moves `amount` out of the source account
and credits `amount - fee` to the destination; `fee` is the processor/bank
charge (recorded as a FEE ledger row). e.g. withdraw 1,000,000 from BANK with
a 5,000 fee → BANK -1,000,000, SAFE +995,000, fee 5,000.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction, IntegrityError
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

    There is no DB-level uniqueness on (kind, is_deleted), so two concurrent
    transactions could both miss the SELECT and each INSERT a row — duplicate
    SAFE/BANK accounts whose balances then diverge. Guard against the race in
    code: catch the IntegrityError from a losing INSERT and re-fetch the row
    the winner created, with the lock held. RECOMMENDED follow-up (separate
    migration, intentionally not added here): a
    UniqueConstraint(fields=['kind'], condition=Q(is_deleted=False)) so the DB
    enforces a single active account per kind."""
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
           performed_by=None):
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
        # get_or_create only collapses concurrent creators into one row when the
        # DB enforces uniqueness on (kind, active); without the recommended
        # UniqueConstraint(kind, condition=is_deleted=False) two callers can
        # still each INSERT an account. .filter(...).first() picks the oldest of
        # any duplicates so the display stays stable until the constraint lands.
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
        """Route an inkassa into the treasury: cash → SAFE, cards → BANK.

        Amounts are non-negative Decimals already validated by the caller.
        Returns (safe_txn, bank_txn) — either may be None when its amount is 0.
        """
        safe_txn = bank_txn = None
        cash_amount = cash_amount or Decimal('0')
        card_amount = card_amount or Decimal('0')
        if cash_amount > 0:
            safe = _get_account_locked(TreasuryAccount.Kind.SAFE)
            safe_txn = _apply(
                safe, cash_amount, TreasuryTransaction.Type.INKASSA,
                description='Inkassa cash collection',
                reference_type='Inkassa', reference_id=reference_id,
                performed_by=performed_by,
            )
        if card_amount > 0:
            bank = _get_account_locked(TreasuryAccount.Kind.BANK)
            bank_txn = _apply(
                bank, card_amount, TreasuryTransaction.Type.INKASSA,
                description='Inkassa card settlement',
                reference_type='Inkassa', reference_id=reference_id,
                performed_by=performed_by,
            )
        return safe_txn, bank_txn

    @staticmethod
    @transaction.atomic
    def deposit_shift(cash_amount, card_amount, performed_by=None, reference_id=None):
        """Post a shift's confirmed settlement into the treasury: cash → SAFE,
        cards → BANK. Returns (safe_txn, bank_txn); either may be None."""
        safe_txn = bank_txn = None
        cash_amount = _to_decimal(cash_amount) or Decimal('0')
        card_amount = _to_decimal(card_amount) or Decimal('0')
        if cash_amount > 0:
            safe = _get_account_locked(TreasuryAccount.Kind.SAFE)
            safe_txn = _apply(
                safe, cash_amount, TreasuryTransaction.Type.SHIFT_DEPOSIT,
                description='Shift cash settlement',
                reference_type='Shift', reference_id=reference_id,
                performed_by=performed_by,
            )
        if card_amount > 0:
            bank = _get_account_locked(TreasuryAccount.Kind.BANK)
            bank_txn = _apply(
                bank, card_amount, TreasuryTransaction.Type.SHIFT_DEPOSIT,
                description='Shift card settlement',
                reference_type='Shift', reference_id=reference_id,
                performed_by=performed_by,
            )
        return safe_txn, bank_txn

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
