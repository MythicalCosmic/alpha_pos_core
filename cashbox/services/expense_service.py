"""Cashbox (drawer) expenses — money paid OUT of a shift's cash drawer.

Recording an expense reduces the (derived) drawer automatically because
drawer_cash() subtracts cashbox expenses. When the recipient is a supplier we
also post a PAYMENT to the supplier ledger so what we owe drops.
"""
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import Inkassa, Shift
from base.repositories import CashRegisterRepository
from cashbox.models import CashboxExpense, CashboxExpenseCategory


def _to_dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _authorize_shift(shift, actor):
    """Fail closed unless actor owns, or manages, this branch's shift."""
    if actor is None or getattr(actor, 'is_deleted', False):
        return ServiceResponse.forbidden('Authenticated staff actor is required')
    actor_branch = str(getattr(actor, 'branch_id', '') or '').strip()
    shift_branch = str(shift.branch_id or '').strip()
    owner_branch = str(getattr(shift.user, 'branch_id', '') or '').strip()
    if not actor_branch or not shift_branch or not owner_branch:
        return ServiceResponse.forbidden('Shift branch ownership is incomplete')
    owner_is_global = owner_branch.lower() == 'cloud'
    actor_is_global = actor_branch.lower() == 'cloud'
    if not owner_is_global and owner_branch != shift_branch:
        return ServiceResponse.forbidden(
            'Shift and cashier belong to different branches',
        )
    if getattr(actor, 'role', None) in ('ADMIN', 'MANAGER'):
        if not actor_is_global and actor_branch != shift_branch:
            return ServiceResponse.forbidden(
                'You can only manage cashbox expenses for your own branch',
            )
    elif (
        actor.id != shift.user_id
        or (not actor_is_global and actor_branch != shift_branch)
    ):
        return ServiceResponse.forbidden(
            'You can only access expenses for your own shift',
        )
    return None


class CashboxExpenseService:

    @staticmethod
    @transaction.atomic
    def create(shift_id, amount, category_id=None, comment='',
               recipient_user_id=None, recipient_supplier_id=None, actor=None,
               created_by=None):
        # Same first lock as ShiftService.end_shift. Whichever request wins is
        # decisive: an expense that locks first is included in the close;
        # otherwise it observes ENDED and is rejected. It can never commit after
        # a close using a stale pre-lock ACTIVE read.
        shift = (
            Shift.objects.select_for_update()
            .select_related('user')
            .filter(id=shift_id, is_deleted=False)
            .first()
        )
        if not shift:
            return ServiceResponse.not_found('Shift not found')
        # ``created_by`` remains a compatibility alias for internal callers;
        # authorization always evaluates an explicit staff identity.
        actor = actor or created_by
        auth_error = _authorize_shift(shift, actor)
        if auth_error:
            return auth_error
        if shift.status != Shift.Status.ACTIVE:
            return ServiceResponse.error('Can only record expenses on an active shift')

        amt = _to_dec(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than 0'})
        if recipient_user_id and recipient_supplier_id:
            return ServiceResponse.validation_error(
                errors={'recipient': 'Choose at most one recipient (user OR supplier)'})

        # The derived shift drawer is the accounting source of truth. Refuse a
        # payout that would make its expected physical cash negative. This also
        # ignores soft-deleted orders/expenses through drawer_cash().
        from cashbox.services.drawer import drawer_cash
        available_drawer = _to_dec(drawer_cash(shift))
        if available_drawer is None or available_drawer < 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Shift drawer balance is invalid'},
                message='Cannot spend from an invalid drawer balance',
            )
        if amt > available_drawer:
            return ServiceResponse.validation_error(
                errors={
                    'amount':
                        f'Amount exceeds available shift cash {available_drawer}',
                },
                message='Insufficient drawer cash',
            )

        branch = str(
            shift.branch_id or getattr(settings, 'BRANCH_ID', '') or ''
        ).strip()
        if not branch:
            return ServiceResponse.validation_error(
                errors={'shift': 'Shift has no branch identity'},
                message='Cannot resolve drawer',
            )

        # Serialize every local payout / cloud command decision with inkassa on
        # the same branch register. On cloud, subtract pending durable commands
        # from the last branch-reported balance; on the branch pending commands
        # have already been applied by sync.
        register = CashRegisterRepository.get_or_create_current(
            branch, for_update=True,
        )
        pending = Inkassa.pending_register_amount(register)
        available_register = (
            register.current_balance or Decimal('0')
        ) - pending
        if amt > available_register:
            return ServiceResponse.validation_error(
                errors={
                    'amount':
                        f'Amount exceeds available register cash {available_register}',
                },
                message='Insufficient register cash',
            )

        category = None
        if category_id:
            category = CashboxExpenseCategory.objects.filter(
                id=category_id, is_deleted=False).first()
            if not category:
                return ServiceResponse.not_found('Expense category not found')

        if recipient_user_id:
            from base.models import User
            recipient = User.objects.filter(
                pk=recipient_user_id,
                is_deleted=False,
                status=User.UserStatus.ACTIVE,
                branch_id=branch,
            ).first()
            if recipient is None:
                return ServiceResponse.validation_error(
                    errors={'recipient_user_id': 'Recipient is not in this branch'},
                )
        if recipient_supplier_id:
            from stock.models import Supplier
            # Suppliers are branch-owned because their balance is branch money,
            # but installations upgraded from the pre-branch schema can still
            # contain an unclaimed (blank branch) supplier.  Claim that legacy
            # row atomically for this shift's branch; never allow a supplier
            # already owned by a different branch to cross the boundary.
            supplier = (
                Supplier.objects.select_for_update()
                .filter(pk=recipient_supplier_id, is_deleted=False)
                .first()
            )
            if supplier is not None and not str(supplier.branch_id or '').strip():
                supplier.branch_id = branch
                supplier.save(update_fields=[
                    'branch_id', 'updated_at', 'synced_at', 'sync_version',
                ])
            if supplier is not None and str(supplier.branch_id) != branch:
                supplier = None
            if supplier is None:
                return ServiceResponse.validation_error(
                    errors={
                        'recipient_supplier_id':
                            'Supplier is not in this branch',
                    },
                )

        is_cloud_command = getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
        stored_comment = (
            CashboxExpense.command_comment(comment)
            if is_cloud_command else (comment or '')
        )
        expense = CashboxExpense.objects.create(
            shift=shift, category=category, amount=amt, comment=stored_comment,
            recipient_user_id=recipient_user_id,
            recipient_supplier_id=recipient_supplier_id,
            created_by=actor,
            branch_id=branch,
            register_command=is_cloud_command,
        )

        # Paying a supplier from the drawer also moves the supplier balance down.
        if recipient_supplier_id:
            from stock.services.supplier_ledger_service import SupplierLedgerService
            SupplierLedgerService.record_drawer_payment(
                recipient_supplier_id, amt, reference_id=expense.id,
                performed_by=actor, note=comment or '',
            )

        if not is_cloud_command:
            register.current_balance = (
                register.current_balance or Decimal('0')
            ) - amt
            register.save(update_fields=[
                'current_balance', 'last_updated', 'synced_at', 'sync_version',
            ])

        return ServiceResponse.created(
            data={'id': expense.id, 'amount': str(amt), 'shift_id': shift.id},
            message='Expense recorded')

    @staticmethod
    def list_for_shift(shift_id, actor=None):
        shift = (
            Shift.objects.select_related('user')
            .filter(pk=shift_id, is_deleted=False)
            .first()
        )
        if shift is None:
            return ServiceResponse.not_found('Shift not found')
        auth_error = _authorize_shift(shift, actor)
        if auth_error:
            return auth_error
        rows = (CashboxExpense.objects.filter(shift_id=shift_id, is_deleted=False)
                .select_related('category', 'recipient_user', 'recipient_supplier'))
        return ServiceResponse.success(data=[{
            'id': e.id,
            'amount': str(e.amount),
            'comment': CashboxExpense.visible_comment(e.comment),
            'category': e.category.name if e.category else None,
            'recipient_user': (f'{e.recipient_user.first_name} {e.recipient_user.last_name}'.strip()
                               if e.recipient_user else None),
            'recipient_supplier': e.recipient_supplier.name if e.recipient_supplier else None,
            'created_at': e.created_at.isoformat() if e.created_at else None,
        } for e in rows])


class CashboxCategoryService:

    @staticmethod
    def list():
        rows = CashboxExpenseCategory.objects.filter(is_deleted=False, is_active=True)
        return ServiceResponse.success(data=[
            {'id': c.id, 'name': c.name, 'sort_order': c.sort_order} for c in rows])

    @staticmethod
    def create(name, sort_order=0):
        if not (name or '').strip():
            return ServiceResponse.validation_error(errors={'name': 'Name is required'})
        c = CashboxExpenseCategory.objects.create(name=name.strip(), sort_order=sort_order or 0)
        return ServiceResponse.created(data={'id': c.id, 'name': c.name})
