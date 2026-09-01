from decimal import Decimal

from django.conf import settings
from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import Inkassa, Shift
from base.money import local_iso, uzs_int, whole_uzs
from base.repositories import CashRegisterRepository
from base.services.branch_scope import resolve_actor_branch
from cashbox.models import CashboxExpense, CashboxExpenseCategory
from hr.models import ExpenseCategory


def _actor_name(actor):
    if actor is None:
        return ''
    return f'{actor.first_name} {actor.last_name}'.strip()


def _authorize_shift(shift, actor):
    if actor is None or getattr(actor, 'is_deleted', False):
        return ServiceResponse.forbidden('Authenticated staff actor is required')
    raw_actor_branch = str(getattr(actor, 'branch_id', '') or '').strip()
    actor_branch = str(resolve_actor_branch(actor) or '').strip()
    shift_branch = str(shift.branch_id or '').strip()
    owner_branch = str(getattr(shift.user, 'branch_id', '') or '').strip()
    global_admin = (
        getattr(actor, 'role', None) == 'ADMIN'
        and raw_actor_branch.lower() in {'', 'cloud'}
    )
    if (not actor_branch and not global_admin) or not shift_branch or not owner_branch:
        return ServiceResponse.forbidden('Shift branch ownership is incomplete')
    if owner_branch.lower() != 'cloud' and owner_branch != shift_branch:
        return ServiceResponse.forbidden('Shift and cashier belong to different branches')
    if getattr(actor, 'role', None) in {'ADMIN', 'MANAGER'}:
        if not global_admin and actor_branch != shift_branch:
            return ServiceResponse.forbidden(
                'You can only manage cashbox expenses for your own branch'
            )
    elif actor.id != shift.user_id or (
        actor_branch != shift_branch
    ):
        return ServiceResponse.forbidden('You can only access expenses for your own shift')
    return None


def _canonical_category(category_id):
    category = ExpenseCategory.objects.filter(
        pk=category_id,
        is_deleted=False,
    ).first()
    if category is not None:
        return category
    legacy = CashboxExpenseCategory.objects.filter(
        pk=category_id,
        is_deleted=False,
    ).select_related('canonical_category').first()
    return legacy.canonical_category if legacy else None


class CashboxExpenseService:
    @classmethod
    @transaction.atomic
    def create_payment(cls, expense, actor, comment='', command_id=None,
                       idempotency_key=''):
        if command_id:
            existing = CashboxExpense.objects.filter(
                command_id=command_id,
                is_deleted=False,
            ).first()
            if existing:
                return ServiceResponse.success(data=cls.serialize(existing))

        shift = Shift.objects.select_for_update().select_related('user').filter(
            pk=expense.shift_id,
            branch_id=expense.branch_id,
            is_deleted=False,
        ).first()
        if shift is None:
            return ServiceResponse.not_found('Shift not found')
        auth_error = _authorize_shift(shift, actor)
        if auth_error:
            return auth_error
        if shift.status != Shift.Status.ACTIVE:
            return ServiceResponse.conflict(
                'DRAWER_SHIFT_NOT_ACTIVE',
                'Drawer expenses require an active shift.',
                errors={'shift_id': [f'Expected ACTIVE but found {shift.status}.']},
            )
        from base.services.shift_device import cashier_shift_device_error

        device_error = cashier_shift_device_error(actor, shift)
        if device_error:
            return ServiceResponse.failure('DRAWER_DEVICE_FORBIDDEN', device_error, 403)
        if expense.category_id is None:
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_REQUIRED',
                'An expense category is required.',
                422,
                errors={'category_id': ['This field is required.']},
            )
        if 'DRAWER' not in (expense.category_allowed_sources_snapshot or []):
            return ServiceResponse.failure(
                'EXPENSE_SOURCE_NOT_ALLOWED',
                'The selected category does not allow drawer payments.',
                422,
            )

        amount = whole_uzs(expense.amount, 'amount_uzs', positive=True)
        from cashbox.services.drawer import drawer_cash

        available_drawer = drawer_cash(shift)
        if available_drawer is None or available_drawer < 0:
            return ServiceResponse.failure(
                'DRAWER_BALANCE_INVALID',
                'The shift drawer balance is not trustworthy.',
                422,
            )
        if amount > available_drawer:
            return ServiceResponse.failure(
                'INSUFFICIENT_FUNDS',
                'The drawer has insufficient funds.',
                422,
                errors={'amount_uzs': ['Available balance is below the required debit.']},
                details={
                    'available_uzs': uzs_int(available_drawer),
                    'required_uzs': uzs_int(amount),
                },
            )

        register = CashRegisterRepository.get_or_create_current(
            shift.branch_id,
            for_update=True,
        )
        pending = Inkassa.pending_register_amount(register)
        available_register = (register.current_balance or Decimal('0')) - pending
        if amount > available_register:
            return ServiceResponse.failure(
                'INSUFFICIENT_FUNDS',
                'The branch register has insufficient funds.',
                422,
                errors={'amount_uzs': ['Available balance is below the required debit.']},
                details={
                    'available_uzs': uzs_int(available_register),
                    'required_uzs': uzs_int(amount),
                },
            )

        cloud_command = getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
        stored_comment = (
            CashboxExpense.command_comment(comment)
            if cloud_command else str(comment or '')
        )
        row = CashboxExpense.objects.create(
            shift=shift,
            canonical_category=expense.category,
            category_code_snapshot=expense.category_code_snapshot,
            category_name_snapshot=expense.category_name_snapshot,
            canonical_expense=expense,
            amount=amount,
            register_command=cloud_command,
            comment=stored_comment,
            recipient_user=expense.subject_user,
            created_by=actor,
            actor_display_snapshot=_actor_name(actor),
            command_id=command_id,
            idempotency_key=idempotency_key or '',
            branch_id=shift.branch_id,
        )
        if not cloud_command:
            register.current_balance = available_register - amount
            register.save(update_fields=[
                'current_balance', 'last_updated', 'synced_at', 'sync_version',
            ])
        return ServiceResponse.created(data=cls.serialize(row))

    @classmethod
    @transaction.atomic
    def reverse_payment(cls, original, actor, reason, command_id=None,
                        idempotency_key=''):
        original = CashboxExpense.objects.select_for_update(of=('self',)).select_related(
            'shift__user', 'canonical_category', 'canonical_expense',
        ).filter(pk=original.pk, is_deleted=False).first()
        if original is None:
            return ServiceResponse.not_found('Cashbox expense not found')
        existing = CashboxExpense.objects.filter(
            reversal_of=original,
            is_deleted=False,
        ).first()
        if existing:
            return ServiceResponse.success(data=cls.serialize(existing))
        shift = Shift.objects.select_for_update().select_related('user').get(
            pk=original.shift_id,
        )
        auth_error = _authorize_shift(shift, actor)
        if auth_error:
            return auth_error
        register = CashRegisterRepository.get_or_create_current(
            shift.branch_id,
            for_update=True,
        )
        cloud_command = getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
        comment = f'VOID: {str(reason or "").strip()}'
        row = CashboxExpense.objects.create(
            shift=shift,
            canonical_category=original.canonical_category,
            category_code_snapshot=original.category_code_snapshot,
            category_name_snapshot=original.category_name_snapshot,
            reversal_of=original,
            amount=-original.amount,
            register_command=cloud_command,
            comment=(
                CashboxExpense.command_comment(comment)
                if cloud_command else comment
            ),
            created_by=actor,
            actor_display_snapshot=_actor_name(actor),
            command_id=command_id,
            idempotency_key=idempotency_key or '',
            branch_id=shift.branch_id,
        )
        if not cloud_command:
            register.current_balance = (
                register.current_balance or Decimal('0')
            ) + original.amount
            register.save(update_fields=[
                'current_balance', 'last_updated', 'synced_at', 'sync_version',
            ])
        return ServiceResponse.created(data=cls.serialize(row))

    @classmethod
    @transaction.atomic
    def create(cls, shift_id, amount, category_id=None, comment='',
               recipient_user_id=None, recipient_supplier_id=None, actor=None,
               created_by=None, command_id=None, idempotency_key=''):
        actor = actor or created_by
        if recipient_supplier_id:
            return ServiceResponse.failure(
                'UNFUNDED_PAYMENT_ROUTE_RETIRED',
                'Supplier payments must use the funded SAFE/BANK payment endpoint.',
                410,
            )
        category = _canonical_category(category_id)
        if category is None:
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_REQUIRED',
                'A canonical expense category is required.',
                422,
                errors={'category_id': ['Select an active category.']},
            )
        from hr.services.expense_service import ExpenseService

        result, status = ExpenseService.direct_pay(
            actor=actor,
            action_id=command_id,
            idempotency_key=idempotency_key,
            amount_uzs=amount,
            category_id=category.id,
            requested_source='DRAWER',
            shift_id=shift_id,
            subject_user_id=recipient_user_id,
            description=comment,
        )
        if status >= 400:
            return result, status
        cashbox_id = result['data'].get('cashbox_expense_id')
        row = CashboxExpense.objects.select_related(
            'canonical_category', 'canonical_expense',
        ).filter(pk=cashbox_id, is_deleted=False).first()
        if row is None:
            transaction.set_rollback(True)
            return ServiceResponse.conflict(
                'EXPENSE_PAYMENT_LINK_MISSING',
                'Drawer payment was not linked to its cashbox record.',
            )
        data = cls.serialize(row)
        data['expense_id'] = row.canonical_expense_id
        data['status'] = row.canonical_expense.status
        return ServiceResponse.created(data=data)

    @staticmethod
    def serialize(row):
        return {
            'id': row.id,
            'amount': str(row.amount),
            'amount_uzs': uzs_int(row.amount),
            'shift_id': row.shift_id,
            'category_id': row.canonical_category_id,
            'category_code': row.category_code_snapshot or None,
            'category_name': row.category_name_snapshot or None,
            'canonical_expense_id': row.canonical_expense_id,
            'reversal_of_id': row.reversal_of_id,
            'comment': CashboxExpense.visible_comment(row.comment),
            'created_at': local_iso(row.created_at),
        }

    @classmethod
    def list_for_shift(cls, shift_id, actor=None):
        shift = Shift.objects.select_related('user').filter(
            pk=shift_id,
            is_deleted=False,
        ).first()
        if shift is None:
            return ServiceResponse.not_found('Shift not found')
        auth_error = _authorize_shift(shift, actor)
        if auth_error:
            return auth_error
        rows = CashboxExpense.objects.filter(
            shift=shift,
            is_deleted=False,
        ).select_related('canonical_category')
        return ServiceResponse.success(data=[cls.serialize(row) for row in rows])


class CashboxCategoryService:
    @staticmethod
    def list():
        from hr.services.expense_category_service import ExpenseCategoryService

        result, status = ExpenseCategoryService.list(per_page=100)
        if status >= 400:
            return result, status
        return ServiceResponse.success(data=result['data']['categories'])

    @staticmethod
    def create(name, sort_order=0, reporting_group='REVIEW', actor=None,
               code=None, allowed_sources=None, **values):
        from hr.services.expense_category_service import ExpenseCategoryService

        result, status = ExpenseCategoryService.create(
            name=name,
            code=code,
            sort_order=sort_order,
            reporting_group=reporting_group,
            allowed_sources=(
                allowed_sources
                if allowed_sources is not None
                else ['DRAWER', 'SAFE', 'BANK']
            ),
            actor=actor,
            **values,
        )
        if status < 400:
            category = result['data']['category']
            result['data'].update(category)
        return result, status
