from typing import Dict, Any, Tuple
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Q

from base.helpers.response import ServiceResponse
from base.models import CashRegister
from base.repositories import CashRegisterRepository
from hr.models import CashTransaction
from hr.repositories import CashTransactionRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class CashTransactionService:

    @staticmethod
    def _amount(value) -> Tuple[Decimal | None, Tuple[Dict[str, Any], int] | None]:
        try:
            amount = Decimal(str(value).strip())
            rounded = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError):
            return None, ServiceResponse.validation_error(
                errors={'amount': 'Amount must be a valid number'},
            )
        if not amount.is_finite() or amount <= 0:
            return None, ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than zero'},
            )
        if amount != rounded:
            return None, ServiceResponse.validation_error(
                errors={'amount': 'Amount cannot have more than two decimal places'},
            )
        # DecimalField(max_digits=12, decimal_places=2).
        if rounded > Decimal('9999999999.99'):
            return None, ServiceResponse.validation_error(
                errors={'amount': 'Amount is too large'},
            )
        return rounded, None

    @staticmethod
    def _payment_method(value: str) -> Tuple[str, Tuple[Dict[str, Any], int] | None]:
        """Normalize and validate before deciding whether drawer cash moves.

        Model ``choices`` are not enforced by ``save()``.  Testing the caller's
        raw string directly against ``"CASH"`` would therefore let values such
        as ``"cash"`` or an unknown method record a cash transaction without
        changing the physical drawer.
        """
        method = str(value or "").strip().upper()
        if method not in CashTransaction.PaymentMethod.values:
            return method, ServiceResponse.validation_error(
                errors={
                    "payment_method": (
                        f"Must be one of {list(CashTransaction.PaymentMethod.values)}"
                    ),
                },
            )
        return method, None

    @staticmethod
    def _locked_branch_register() -> CashRegister:
        """Lock only the configured branch's accounting cursor.

        Every ledger row carries the branch cash cursor before/after the event,
        including non-cash events. Taking the branch-scoped row lock keeps that
        snapshot coherent while still mutating it only for CASH.
        """
        return CashRegisterRepository.get_or_create_current(for_update=True)

    @staticmethod
    def _unresolved_shift_error(register, payment_method):
        """Refuse generic cash mutations while a shift owns branch cash.

        Shift handover derives its expected cash only from paid orders, refunds
        and ``CashboxExpense`` rows. A generic HR cash deposit/withdrawal used
        to change ``CashRegister`` without changing any shift, so the physical
        drawer and the close report could diverge invisibly.
        """
        if payment_method != CashTransaction.PaymentMethod.CASH:
            return None
        from base.models import Shift

        unresolved = list(
            Shift.objects.filter(
                is_deleted=False,
                branch_id=register.branch_id,
                status__in=(Shift.Status.ACTIVE, Shift.Status.ENDED),
            )
            .order_by('start_time', 'id')
            .values_list('id', 'status')[:10]
        )
        if not unresolved:
            return None
        details = ', '.join(
            f'{shift_id} ({status})' for shift_id, status in unresolved
        )
        return ServiceResponse.validation_error(
            errors={
                'cash_drawer': (
                    'Cash is owned by unresolved shift(s): '
                    f'{details}. Record an active-shift payout through the '
                    'cashbox expense flow, or reconcile the shift first.'
                ),
            },
            message='Direct cash movement is blocked while a shift drawer is open',
        )

    @classmethod
    def _serialize(cls, txn: CashTransaction) -> Dict[str, Any]:
        data = {
            "id": txn.id,
            "uuid": str(txn.uuid),
            "type": txn.type,
            "type_display": txn.get_type_display(),
            "amount": str(txn.amount),
            "description": txn.description,
            "payment_method": txn.payment_method,
            "payment_method_display": txn.get_payment_method_display(),
            "reference_type": txn.reference_type,
            "reference_id": txn.reference_id,
            "balance_before": str(txn.balance_before),
            "balance_after": str(txn.balance_after),
            "performed_by_id": txn.performed_by_id,
            "approved_by_id": txn.approved_by_id,
            "notes": txn.notes,
            "created_at": txn.created_at.isoformat(),
        }

        if hasattr(txn, '_performed_by_cache') or (
            txn.performed_by_id and hasattr(txn, 'performed_by')
            and txn.performed_by is not None
        ):
            try:
                user = txn.performed_by
                data["performed_by"] = {
                    "id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                }
            except Exception:
                pass

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             type: str = None,
             date_from=None,
             date_to=None,
             search: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = CashTransaction.objects.filter(
            is_deleted=False
        ).select_related("performed_by")

        if type:
            queryset = queryset.filter(type=type)

        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        if search:
            queryset = queryset.filter(
                Q(description__icontains=search)
                | Q(reference_type__icontains=search)
                | Q(notes__icontains=search)
                | Q(performed_by__first_name__icontains=search)
                | Q(performed_by__last_name__icontains=search)
            )

        queryset = queryset.order_by("-created_at")

        page_obj, paginator = CashTransactionRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "transactions": [cls._serialize(txn) for txn in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "types": [
                {"value": c[0], "label": c[1]}
                for c in CashTransaction.TransactionType.choices
            ],
        })

    @classmethod
    def get(cls, transaction_id: int) -> Tuple[Dict[str, Any], int]:
        txn = CashTransactionRepository.get_with_relations(transaction_id)
        if not txn:
            return ServiceResponse.not_found(
                f"Cash transaction with id {transaction_id} not found"
            )

        return ServiceResponse.success(data={
            "transaction": cls._serialize(txn),
        })

    @classmethod
    @transaction.atomic
    def deposit(cls,
                amount: Decimal,
                description: str = "",
                payment_method: str = "CASH",
                performed_by_id: int = None,
                notes: str = "") -> Tuple[Dict[str, Any], int]:
        amount, amount_error = cls._amount(amount)
        if amount_error:
            return amount_error

        payment_method, method_error = cls._payment_method(payment_method)
        if method_error:
            return method_error

        register = cls._locked_branch_register()
        shift_error = cls._unresolved_shift_error(register, payment_method)
        if shift_error:
            return shift_error

        balance_before = register.current_balance
        if payment_method == CashTransaction.PaymentMethod.CASH:
            new_balance = register.current_balance + amount
            if new_balance > Decimal('9999999999.99'):
                return ServiceResponse.validation_error(
                    errors={'amount': 'Deposit would exceed the register limit'},
                )
            register.current_balance = new_balance
            register.save(update_fields=["current_balance", "last_updated"])

        txn = CashTransactionRepository.create(
            type=CashTransaction.TransactionType.DEPOSIT,
            amount=amount,
            description=description,
            payment_method=payment_method,
            balance_before=balance_before,
            balance_after=register.current_balance,
            branch_id=register.branch_id,
            performed_by_id=performed_by_id,
            notes=notes,
        )

        return ServiceResponse.created(data={
            "id": txn.id,
            "transaction": cls._serialize(txn),
        }, message="Deposit recorded")

    @classmethod
    @transaction.atomic
    def withdraw(cls,
                 amount: Decimal,
                 description: str = "",
                 payment_method: str = "CASH",
                 performed_by_id: int = None,
                 notes: str = "") -> Tuple[Dict[str, Any], int]:
        amount, amount_error = cls._amount(amount)
        if amount_error:
            return amount_error

        payment_method, method_error = cls._payment_method(payment_method)
        if method_error:
            return method_error

        register = cls._locked_branch_register()
        shift_error = cls._unresolved_shift_error(register, payment_method)
        if shift_error:
            return shift_error
        if (payment_method == CashTransaction.PaymentMethod.CASH
                and register.current_balance < amount):
            return ServiceResponse.error(
                f"Insufficient cash balance. Available: {register.current_balance}"
            )

        balance_before = register.current_balance
        if payment_method == CashTransaction.PaymentMethod.CASH:
            register.current_balance -= amount
            register.save(update_fields=["current_balance", "last_updated"])

        txn = CashTransactionRepository.create(
            type=CashTransaction.TransactionType.WITHDRAWAL,
            amount=amount,
            description=description,
            payment_method=payment_method,
            balance_before=balance_before,
            balance_after=register.current_balance,
            branch_id=register.branch_id,
            performed_by_id=performed_by_id,
            notes=notes,
        )

        return ServiceResponse.created(data={
            "id": txn.id,
            "transaction": cls._serialize(txn),
        }, message="Withdrawal recorded")

    @classmethod
    @transaction.atomic
    def create_for_reference(cls,
                             type: str,
                             amount: Decimal,
                             description: str = "",
                             payment_method: str = "CASH",
                             reference_type: str = "",
                             reference_id: int = None,
                             performed_by_id: int = None,
                             notes: str = "") -> Tuple[Dict[str, Any], int]:
        amount, amount_error = cls._amount(amount)
        if amount_error:
            return amount_error

        type = str(type or '').strip().upper()
        if type not in CashTransaction.TransactionType.values:
            return ServiceResponse.validation_error(
                errors={
                    'type': f'Must be one of {list(CashTransaction.TransactionType.values)}',
                },
            )

        payment_method, method_error = cls._payment_method(payment_method)
        if method_error:
            return method_error

        register = cls._locked_branch_register()
        shift_error = cls._unresolved_shift_error(register, payment_method)
        if shift_error:
            return shift_error
        balance_before = register.current_balance
        balance_after = balance_before

        if payment_method == CashTransaction.PaymentMethod.CASH:
            if register.current_balance < amount:
                return ServiceResponse.error(
                    f"Insufficient cash balance. Available: {register.current_balance}"
                )

            register.current_balance -= amount
            register.save(update_fields=["current_balance", "last_updated"])
            balance_after = register.current_balance

        txn = CashTransactionRepository.create(
            type=type,
            amount=amount,
            description=description,
            payment_method=payment_method,
            reference_type=reference_type,
            reference_id=reference_id,
            balance_before=balance_before,
            balance_after=balance_after,
            branch_id=register.branch_id,
            performed_by_id=performed_by_id,
            notes=notes,
        )

        return ServiceResponse.created(data={
            "id": txn.id,
            "transaction": cls._serialize(txn),
        }, message=f"{type} transaction recorded")

    @classmethod
    def get_balance_summary(cls,
                            date_from=None,
                            date_to=None) -> Tuple[Dict[str, Any], int]:
        register = CashRegisterRepository.get_current()
        current_balance = str(register.current_balance) if register else "0.00"

        totals_by_type = {}
        if date_from and date_to:
            totals_by_type = CashTransactionRepository.get_balance_summary(
                date_from, date_to
            )
        else:
            qs = CashTransaction.objects.filter(is_deleted=False)
            from django.db.models import Sum
            raw = dict(
                qs.values_list("type").annotate(total=Sum("amount"))
            )
            totals_by_type = raw

        serialized_totals = {
            k: str(v) for k, v in totals_by_type.items()
        }

        return ServiceResponse.success(data={
            "current_balance": current_balance,
            "totals_by_type": serialized_totals,
        })
