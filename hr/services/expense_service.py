from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.financial import EXPENSE_REPORTING_GROUPS
from base.helpers.response import ServiceResponse
from base.models import TreasuryTransaction, User
from base.money import (
    MoneyValueError,
    local_iso,
    percentage,
    percentage_fee,
    uzs_int,
    whole_uzs,
)
from base.services.branch_scope import resolve_actor_branch
from hr.models import Expense, ExpenseCategory, ExpenseTransition


def _actor_snapshot(actor):
    if actor is None:
        return ''
    return f'{actor.first_name} {actor.last_name}'.strip()


def _actor_data(actor):
    if actor is None:
        return None
    return {'id': actor.id, 'name': _actor_snapshot(actor)}


def _parse_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _transition(
    expense,
    previous_status,
    new_status,
    actor,
    *,
    reason='',
    idempotency_key='',
    metadata=None,
):
    evidence = {
        'expense_uuid': str(expense.uuid),
        'amount_uzs': uzs_int(expense.amount),
        'fee_uzs': uzs_int(expense.fee_uzs),
        'source_account': expense.requested_source or None,
        'shift_id': expense.shift_id,
        'category_id': expense.category_id,
        'category_code': expense.category_code_snapshot or None,
        'category_name': expense.category_name_snapshot or None,
        'category_parent_code': expense.category_parent_code_snapshot or None,
        'category_parent_name': expense.category_parent_name_snapshot or None,
        'category_cost_behavior': expense.category_cost_behavior_snapshot,
        'category_reporting_group': (
            expense.category_reporting_group_snapshot or None
        ),
        **(metadata or {}),
    }
    return ExpenseTransition.objects.create(
        expense=expense,
        previous_status=previous_status or '',
        new_status=new_status,
        actor=actor,
        actor_display_snapshot=_actor_snapshot(actor),
        reason=reason or '',
        idempotency_key=idempotency_key or '',
        metadata=evidence,
        branch_id=expense.branch_id,
    )


def _cashbox_payment(expense):
    try:
        return expense.cashbox_payment
    except Exception:
        return None


def _category_snapshot(category):
    parent = category.parent if category.parent_id else None
    return {
        'category': category,
        'category_code_snapshot': category.code,
        'category_name_snapshot': category.name,
        'category_allowed_sources_snapshot': [
            str(value).upper() for value in (category.allowed_sources or [])
        ],
        'category_parent_code_snapshot': parent.code if parent else '',
        'category_parent_name_snapshot': parent.name if parent else '',
        'category_cost_behavior_snapshot': category.cost_behavior,
        'category_reporting_group_snapshot': category.reporting_group,
    }


def _expense_category_evidence(expense):
    return {
        'category_id': expense.category_id,
        'code': expense.category_code_snapshot or None,
        'name': expense.category_name_snapshot or None,
        'parent_code': expense.category_parent_code_snapshot or None,
        'parent_name': expense.category_parent_name_snapshot or None,
        'cost_behavior': expense.category_cost_behavior_snapshot,
        'reporting_group': expense.category_reporting_group_snapshot or None,
    }


def _category_assignment_evidence(category):
    parent = category.parent if category.parent_id else None
    return {
        'category_id': category.id,
        'code': category.code,
        'name': category.name,
        'parent_code': parent.code if parent else None,
        'parent_name': parent.name if parent else None,
        'cost_behavior': category.cost_behavior,
        'reporting_group': category.reporting_group,
    }


def _reclassification_summary(
    expenses,
    requested_ids,
    target,
    before_by_expense,
    *,
    dry_run,
    mutation_applied,
):
    by_source = {}
    by_category = {}
    amount = Decimal('0')
    for expense in expenses:
        amount += expense.amount
        by_source[expense.requested_source] = (
            by_source.get(expense.requested_source, 0) + 1
        )
        before = before_by_expense[expense.id]
        key = str(before.get('category_id') or '')
        row = by_category.setdefault(key, {
            'category_id': before.get('category_id'),
            'category_name': before.get('name'),
            'count': 0,
            'amount_uzs': 0,
        })
        row['count'] += 1
        row['amount_uzs'] += uzs_int(expense.amount)
    return {
        'dry_run': dry_run,
        'mutation_applied': mutation_applied,
        'expense_count': len(expenses),
        'amount_uzs': uzs_int(amount),
        'expense_ids': requested_ids,
        'target_category': {
            'id': target['category_id'],
            'code': target['code'],
            'name': target['name'],
            'path': [
                *([target['parent_name']] if target.get('parent_name') else []),
                target['name'],
            ],
            'cost_behavior': target['cost_behavior'],
            'reporting_group': target['reporting_group'],
        },
        'current_categories': list(by_category.values()),
        'by_source': by_source,
    }


class ExpenseService:
    @classmethod
    def serialize(cls, expense, *, include_transitions=False):
        category = None
        if expense.category_id:
            parent = None
            if (
                expense.category_parent_code_snapshot
                or expense.category_parent_name_snapshot
            ):
                parent = {
                    'code': expense.category_parent_code_snapshot or None,
                    'name': expense.category_parent_name_snapshot or None,
                }
            category_name = (
                expense.category_name_snapshot or expense.category.name
            )
            category = {
                'id': expense.category_id,
                'code': expense.category_code_snapshot or expense.category.code,
                'name': category_name,
                'parent': parent,
                'path': [
                    *([parent['name']] if parent and parent['name'] else []),
                    category_name,
                ],
                'cost_behavior': expense.category_cost_behavior_snapshot,
                'reporting_group': (
                    expense.category_reporting_group_snapshot
                    or expense.category.reporting_group
                ),
                'is_active': expense.category.is_active,
            }
        elif expense.category_name_snapshot or expense.category_code_snapshot:
            category = {
                'id': None,
                'code': expense.category_code_snapshot or None,
                'name': expense.category_name_snapshot or None,
                'parent': ({
                    'code': expense.category_parent_code_snapshot or None,
                    'name': expense.category_parent_name_snapshot or None,
                } if (
                    expense.category_parent_code_snapshot
                    or expense.category_parent_name_snapshot
                ) else None),
                'path': [
                    *(
                        [expense.category_parent_name_snapshot]
                        if expense.category_parent_name_snapshot else []
                    ),
                    expense.category_name_snapshot,
                ],
                'cost_behavior': expense.category_cost_behavior_snapshot,
                'reporting_group': (
                    expense.category_reporting_group_snapshot or None
                ),
                'is_active': False,
            }
        cashbox = _cashbox_payment(expense)
        payment = None
        if expense.treasury_transaction_id:
            payment = {
                'type': 'TREASURY',
                'transaction_id': expense.treasury_transaction_id,
                'source_account': expense.requested_source,
            }
        elif cashbox is not None:
            payment = {
                'type': 'DRAWER',
                'cashbox_expense_id': cashbox.id,
                'shift_id': cashbox.shift_id,
            }
        data = {
            'id': expense.id,
            'uuid': str(expense.uuid),
            'category': category,
            'category_id': expense.category_id,
            'category_code_snapshot': expense.category_code_snapshot,
            'category_name_snapshot': expense.category_name_snapshot,
            'category_parent_code_snapshot': (
                expense.category_parent_code_snapshot
            ),
            'category_parent_name_snapshot': (
                expense.category_parent_name_snapshot
            ),
            'category_cost_behavior_snapshot': (
                expense.category_cost_behavior_snapshot
            ),
            'category_reporting_group_snapshot': (
                expense.category_reporting_group_snapshot
            ),
            'amount': str(expense.amount),
            'amount_uzs': uzs_int(expense.amount),
            'fee_uzs': uzs_int(expense.fee_uzs),
            'fee_percent': (
                format(expense.fee_percent, 'f')
                if expense.fee_percent is not None else None
            ),
            'total_debited_uzs': uzs_int(expense.amount + expense.fee_uzs),
            'description': expense.description,
            'expense_date': expense.expense_date.isoformat(),
            'requested_source': expense.requested_source or None,
            'source_account': expense.requested_source or None,
            'shift_id': expense.shift_id,
            'subject_user': _actor_data(expense.subject_user),
            'payment_method': expense.payment_method,
            'status': expense.status,
            'receipt_number': expense.receipt_number,
            'receipt': {
                'has_file': bool(expense.receipt_file),
                'download_path': (
                    f'/api/admins/hr/documents/file/expense/{expense.id}/'
                    if expense.receipt_file else None
                ),
            },
            'created_by': _actor_data(expense.created_by),
            'approved_by': _actor_data(expense.approved_by),
            'paid_by': _actor_data(expense.paid_by),
            'canceled_by': _actor_data(expense.canceled_by),
            'voided_by': _actor_data(expense.voided_by),
            'payment': payment,
            'treasury_reversal_id': expense.treasury_reversal_id,
            'notes': expense.notes,
            'cancel_reason': expense.cancel_reason,
            'void_reason': expense.void_reason,
            'approved_at': (
                local_iso(expense.approved_at)
            ),
            'rejected_at': (
                local_iso(expense.rejected_at)
            ),
            'paid_at': local_iso(expense.paid_at),
            'canceled_at': (
                local_iso(expense.canceled_at)
            ),
            'voided_at': (
                local_iso(expense.voided_at)
            ),
            'created_at': local_iso(expense.created_at),
            'updated_at': local_iso(expense.updated_at),
        }
        if include_transitions:
            data['transitions'] = [{
                'id': row.id,
                'previous_status': row.previous_status or None,
                'new_status': row.new_status,
                'actor': {
                    'id': row.actor_id,
                    'name': row.actor_display_snapshot or None,
                } if row.actor_id or row.actor_display_snapshot else None,
                'reason': row.reason,
                'metadata': row.metadata,
                'created_at': local_iso(row.created_at),
            } for row in expense.transitions.select_related('actor').all()]
        return data

    @classmethod
    def _queryset(cls):
        return Expense.objects.filter(is_deleted=False).select_related(
            'category', 'created_by', 'approved_by', 'paid_by',
            'canceled_by', 'voided_by', 'subject_user',
            'treasury_transaction', 'treasury_reversal',
            'cashbox_payment',
        )

    @classmethod
    def list(
        cls,
        page=1,
        per_page=20,
        status=None,
        category_id=None,
        category_parent_id=None,
        include_subcategories=False,
        cost_behavior=None,
        reporting_group=None,
        source_account=None,
        date_from=None,
        date_to=None,
        search=None,
        *,
        actor=None,
        view_all=False,
        branch_id=None,
    ):
        branch_id = str(branch_id or resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403,
            )
        if not isinstance(include_subcategories, bool):
            return ServiceResponse.validation_error({
                'include_subcategories': ['Use a JSON boolean.'],
            })
        queryset = cls._queryset().filter(branch_id=branch_id)
        if not view_all:
            queryset = queryset.filter(created_by=actor)
        if status:
            statuses = [
                value.strip().upper() for value in str(status).split(',')
                if value.strip()
            ]
            invalid = sorted(set(statuses) - set(Expense.Status.values))
            if invalid:
                return ServiceResponse.validation_error({
                    'status': [f'Unknown status: {", ".join(invalid)}.'],
                })
            queryset = queryset.filter(status__in=statuses)
        if category_id is not None:
            category_filter = Q(category_id=category_id)
            if include_subcategories:
                category_filter |= Q(category__parent_id=category_id)
            queryset = queryset.filter(category_filter)
        if category_parent_id is not None:
            queryset = queryset.filter(category__parent_id=category_parent_id)
        if cost_behavior:
            normalized_behavior = str(cost_behavior).strip().upper()
            if normalized_behavior not in ExpenseCategory.CostBehavior.values:
                return ServiceResponse.validation_error({
                    'cost_behavior': ['Unknown cost behavior.'],
                })
            queryset = queryset.filter(
                category_cost_behavior_snapshot=normalized_behavior,
            )
        if reporting_group:
            normalized_group = str(reporting_group).strip().upper()
            if normalized_group not in EXPENSE_REPORTING_GROUPS:
                return ServiceResponse.validation_error({
                    'reporting_group': ['Unknown financial reporting group.'],
                })
            queryset = queryset.filter(
                Q(category_reporting_group_snapshot=normalized_group)
                | Q(
                    category_reporting_group_snapshot='',
                    category__reporting_group=normalized_group,
                )
            )
        if source_account:
            normalized_source = str(source_account).strip().upper()
            if normalized_source not in Expense.Source.values:
                return ServiceResponse.validation_error({
                    'source_account': ['Must be DRAWER, SAFE, or BANK.'],
                })
            queryset = queryset.filter(requested_source=normalized_source)
        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)
        if search:
            queryset = queryset.filter(
                Q(description__icontains=search)
                | Q(receipt_number__icontains=search)
                | Q(category_name_snapshot__icontains=search)
                | Q(category_code_snapshot__icontains=search)
                | Q(notes__icontains=search)
            )

        totals = queryset.values('status').annotate(
            amount=Coalesce(
                Sum(F('amount') + F('fee_uzs')),
                Decimal('0'),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            count=Count('id'),
        )
        by_status = {
            row['status']: {
                'amount_uzs': uzs_int(row['amount']),
                'count': row['count'],
            }
            for row in totals
        }
        total = queryset.count()
        rows = queryset.order_by('-expense_date', '-created_at', '-id')[
            (page - 1) * per_page:page * per_page
        ]
        return ServiceResponse.success(data={
            'expenses': [cls.serialize(expense) for expense in rows],
            'totals': {
                'row_count': total,
                'by_status': by_status,
                'amount_uzs': sum(
                    row['amount_uzs'] for row in by_status.values()
                ),
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            },
        })

    @classmethod
    def get(cls, expense_id, *, actor=None, view_all=True, branch_id=None):
        branch_id = str(branch_id or resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403,
            )
        queryset = cls._queryset().filter(pk=expense_id)
        queryset = queryset.filter(branch_id=branch_id)
        if actor is not None and not view_all:
            queryset = queryset.filter(created_by=actor)
        expense = queryset.first()
        if expense is None:
            return ServiceResponse.not_found('Expense not found')
        return ServiceResponse.success(data={
            'expense': cls.serialize(expense, include_transitions=True),
        })

    @classmethod
    @transaction.atomic
    def create(
        cls,
        amount_uzs=None,
        expense_date=None,
        category_id=None,
        description='',
        requested_source=None,
        shift_id=None,
        receipt_number='',
        receipt_image_url='',
        subject_user_id=None,
        notes='',
        actor=None,
        created_by_id=None,
        amount=None,
        payment_method=None,
        branch_id=None,
        **_ignored,
    ):
        actor = actor or User.objects.filter(pk=created_by_id).first()
        requested_branch = str(branch_id or '').strip()
        actor_branch = str(resolve_actor_branch(actor) or '').strip()
        if actor is not None and requested_branch and requested_branch != actor_branch:
            return ServiceResponse.failure(
                'LOCATION_FORBIDDEN',
                'Expense branch is outside the authorized scope.',
                403,
            )
        branch_id = actor_branch if actor is not None else requested_branch
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403,
            )
        raw_amount = amount_uzs if amount_uzs is not None else amount
        try:
            amount_value = whole_uzs(
                raw_amount,
                'amount_uzs',
                positive=True,
                maximum=Decimal('9999999999'),
            )
        except MoneyValueError as exc:
            return ServiceResponse.failure(
                'EXPENSE_AMOUNT_INVALID',
                'Expense amount is invalid.',
                422,
                errors={'amount_uzs': [str(exc)]},
            )
        if category_id in (None, ''):
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_REQUIRED',
                'An expense category is required.',
                422,
                errors={'category_id': ['This field is required.']},
            )
        category = ExpenseCategory.objects.select_for_update(
            of=('self',),
        ).filter(
            pk=category_id,
            is_deleted=False,
        ).select_related('parent').first()
        if category is None:
            return ServiceResponse.not_found('Expense category not found')
        if not category.is_active:
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_INACTIVE',
                'Inactive categories cannot be used for new expenses.',
                422,
                errors={'category_id': ['Category is inactive.']},
            )
        if ExpenseCategory.objects.filter(
            parent_id=category.id,
            is_deleted=False,
            is_active=True,
        ).exists():
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_GROUP_ONLY',
                'Choose a subcategory for this expense.',
                422,
                errors={'category_id': ['This category contains subcategories.']},
            )
        source = str(requested_source or '').strip().upper()
        if not source and payment_method:
            source = (
                Expense.Source.DRAWER
                if str(payment_method).upper() == 'CASH'
                else Expense.Source.BANK
            )
        if source not in Expense.Source.values:
            return ServiceResponse.validation_error({
                'requested_source': ['Must be DRAWER, SAFE, or BANK.'],
            })
        allowed_sources = [
            str(value).upper() for value in (category.allowed_sources or [])
        ]
        if source not in allowed_sources:
            return ServiceResponse.failure(
                'EXPENSE_SOURCE_NOT_ALLOWED',
                'The selected category does not allow this payment source.',
                422,
                errors={'requested_source': ['Source is not allowed by category.']},
            )
        if source == Expense.Source.DRAWER and not shift_id:
            return ServiceResponse.validation_error({
                'shift_id': ['A shift is required for DRAWER expenses.'],
            })
        if source != Expense.Source.DRAWER and shift_id:
            return ServiceResponse.validation_error({
                'shift_id': ['Shift is allowed only for DRAWER expenses.'],
            })
        parsed_date = _parse_date(expense_date or timezone.localdate())
        if parsed_date is None:
            return ServiceResponse.validation_error({
                'expense_date': ['Use YYYY-MM-DD.'],
            })
        description = str(description or '').strip()
        receipt_number = str(receipt_number or '').strip()
        if category.requires_description and not description:
            return ServiceResponse.validation_error({
                'description': ['Description is required by this category.'],
            })
        if category.requires_receipt and not (receipt_number or receipt_image_url):
            return ServiceResponse.validation_error({
                'receipt_number': ['Receipt evidence is required by this category.'],
            })
        shift = None
        if shift_id:
            from base.models import Shift

            shift = Shift.objects.filter(
                pk=shift_id,
                branch_id=branch_id,
                is_deleted=False,
            ).first()
            if shift is None:
                return ServiceResponse.failure(
                    'LOCATION_FORBIDDEN',
                    'Shift is not in the authorized branch.',
                    403,
                )
        subject_user = None
        if subject_user_id:
            subject_user = User.objects.filter(
                pk=subject_user_id,
                branch_id=branch_id,
                is_deleted=False,
            ).first()
            if subject_user is None:
                return ServiceResponse.failure(
                    'EXPENSE_SUBJECT_FORBIDDEN',
                    'Expense subject is not in the authorized branch.',
                    403,
                )
        legacy_method = (
            Expense.PaymentMethod.CASH
            if source == Expense.Source.DRAWER
            else Expense.PaymentMethod.BANK_TRANSFER
        )
        expense = Expense.objects.create(
            **_category_snapshot(category),
            amount=amount_value,
            description=description,
            expense_date=parsed_date,
            payment_method=legacy_method,
            status=Expense.Status.PENDING,
            requested_source=source,
            shift=shift,
            subject_user=subject_user,
            receipt_number=receipt_number,
            receipt_image_url=receipt_image_url or '',
            created_by=actor,
            notes=str(notes or '').strip(),
            branch_id=branch_id,
        )
        _transition(expense, '', Expense.Status.PENDING, actor)
        expense = cls._queryset().get(pk=expense.pk)
        return ServiceResponse.created(data={
            'expense_id': expense.id,
            'expense': cls.serialize(expense, include_transitions=True),
        }, message='Expense request created')

    @classmethod
    @transaction.atomic
    def approve(cls, expense_id, approved_by_id=None, *, actor=None, direct=False):
        actor = actor or User.objects.filter(pk=approved_by_id).first()
        expense = cls._locked(expense_id, actor)
        if isinstance(expense, tuple):
            return expense
        if expense.status != Expense.Status.PENDING:
            return cls._state_conflict(expense, Expense.Status.PENDING)
        if not direct and expense.created_by_id == getattr(actor, 'id', None):
            return ServiceResponse.failure(
                'EXPENSE_SELF_APPROVAL_FORBIDDEN',
                'Requester cannot approve their own expense.',
                403,
            )
        if (
            not direct
            and expense.created_by
            and expense.created_by.role == User.RoleChoices.WAREHOUSE
            and expense.subject_user_id == getattr(actor, 'id', None)
        ):
            return ServiceResponse.failure(
                'EXPENSE_SECOND_REVIEWER_REQUIRED',
                'A second reviewer must approve this warehouse request.',
                403,
            )
        now = timezone.now()
        expense.status = Expense.Status.APPROVED
        expense.approved_by = actor
        expense.approved_at = now
        expense.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
        _transition(
            expense,
            Expense.Status.PENDING,
            Expense.Status.APPROVED,
            actor,
            metadata={'direct_adapter': True} if direct else None,
        )
        return ServiceResponse.success(data={
            'expense': cls.serialize(expense, include_transitions=True),
        }, message='Expense approved')

    @classmethod
    @transaction.atomic
    def reject(cls, expense_id, approved_by_id=None, notes='', *, actor=None, reason=None):
        actor = actor or User.objects.filter(pk=approved_by_id).first()
        reason = str(reason or notes or '').strip()
        if not reason:
            return ServiceResponse.validation_error({'reason': ['This field is required.']})
        expense = cls._locked(expense_id, actor)
        if isinstance(expense, tuple):
            return expense
        if expense.status != Expense.Status.PENDING:
            return cls._state_conflict(expense, Expense.Status.PENDING)
        if expense.created_by_id == getattr(actor, 'id', None):
            return ServiceResponse.failure(
                'EXPENSE_SELF_APPROVAL_FORBIDDEN',
                'Requester cannot reject their own expense.',
                403,
            )
        expense.status = Expense.Status.REJECTED
        expense.approved_by = actor
        expense.rejected_at = timezone.now()
        expense.save(update_fields=[
            'status', 'approved_by', 'rejected_at', 'updated_at',
        ])
        _transition(
            expense,
            Expense.Status.PENDING,
            Expense.Status.REJECTED,
            actor,
            reason=reason,
        )
        return ServiceResponse.success(data={
            'expense': cls.serialize(expense, include_transitions=True),
        }, message='Expense rejected')

    @classmethod
    @transaction.atomic
    def pay(
        cls,
        expense_id,
        *,
        actor,
        source_account=None,
        fee_uzs=None,
        fee_percent=None,
        note='',
        action_id=None,
        idempotency_key='',
    ):
        expense = cls._locked(expense_id, actor)
        if isinstance(expense, tuple):
            return expense
        if expense.status == Expense.Status.PAID:
            if action_id and expense.payment_action_id == action_id:
                return ServiceResponse.success(data=cls._payment_result(expense))
            return ServiceResponse.conflict(
                'EXPENSE_ALREADY_PAID',
                'This expense has already been paid.',
                errors={'status': [f'Expected APPROVED but found {expense.status}.']},
                details={'expense_id': expense.id},
            )
        if expense.status != Expense.Status.APPROVED:
            return cls._state_conflict(expense, Expense.Status.APPROVED)
        source = str(source_account or expense.requested_source or '').upper()
        if source not in Expense.Source.values:
            return ServiceResponse.validation_error({
                'source_account': ['Must be DRAWER, SAFE, or BANK.'],
            })
        if source != expense.requested_source:
            return ServiceResponse.validation_error({
                'source_account': ['Must match the approved requested source.'],
            })
        if fee_uzs not in (None, '') and fee_percent not in (None, ''):
            return ServiceResponse.validation_error({
                'fee_uzs': ['Submit either fee_uzs or fee_percent, not both.'],
                'fee_percent': ['Submit either fee_uzs or fee_percent, not both.'],
            })
        try:
            if fee_percent not in (None, ''):
                percent_value = percentage(fee_percent)
                fee_value = percentage_fee(expense.amount, percent_value)
            else:
                percent_value = None
                fee_value = whole_uzs(
                    fee_uzs or 0,
                    'fee_uzs',
                    maximum=Decimal('9999999999'),
                )
        except MoneyValueError as exc:
            field = 'fee_percent' if fee_percent not in (None, '') else 'fee_uzs'
            return ServiceResponse.validation_error({field: [str(exc)]})
        if source != Expense.Source.BANK and fee_value:
            return ServiceResponse.failure(
                'FEE_BANK_ONLY',
                'A fee is allowed only for BANK payments.',
                422,
                errors={
                    'fee_uzs' if fee_percent in (None, '') else 'fee_percent': [
                        'Fee must be zero for DRAWER or SAFE.'
                    ],
                },
            )

        payment_id = None
        treasury_id = None
        if source in {Expense.Source.SAFE, Expense.Source.BANK}:
            from base.services.treasury_service import TreasuryService

            result, status = TreasuryService.record_expense(
                account_kind=source,
                amount=expense.amount,
                fee=fee_value,
                canonical_category=expense.category,
                description=note or expense.description,
                performed_by=actor,
                txn_type=TreasuryTransaction.Type.EXPENSE,
                reference_type='Expense',
                reference_id=expense.id,
                branch_id=expense.branch_id,
                command_id=action_id,
                idempotency_key=idempotency_key,
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            treasury_id = result['data']['transaction']['id']
            expense.treasury_transaction_id = treasury_id
        else:
            from cashbox.services.expense_service import CashboxExpenseService

            result, status = CashboxExpenseService.create_payment(
                expense=expense,
                actor=actor,
                comment=note or expense.description,
                command_id=action_id,
                idempotency_key=idempotency_key,
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            payment_id = result['data']['id']

        now = timezone.now()
        expense.status = Expense.Status.PAID
        expense.paid_by = actor
        expense.paid_at = now
        expense.fee_uzs = fee_value
        expense.fee_percent = percent_value
        expense.payment_action_id = action_id
        expense.notes = '\n'.join(
            value for value in [expense.notes, str(note or '').strip()] if value
        )
        expense.save(update_fields=[
            'status', 'paid_by', 'paid_at', 'fee_uzs', 'fee_percent',
            'payment_action_id', 'treasury_transaction', 'notes', 'updated_at',
        ])
        _transition(
            expense,
            Expense.Status.APPROVED,
            Expense.Status.PAID,
            actor,
            idempotency_key=idempotency_key,
            metadata={
                'amount_uzs': uzs_int(expense.amount),
                'fee_uzs': uzs_int(fee_value),
                'source_account': source,
                'treasury_transaction_id': treasury_id,
                'cashbox_expense_id': payment_id,
            },
        )
        expense = cls._queryset().get(pk=expense.pk)
        return ServiceResponse.success(data=cls._payment_result(expense))

    @classmethod
    @transaction.atomic
    def cancel(cls, expense_id, *, actor, can_approve=False, reason=''):
        expense = cls._locked(expense_id, actor)
        if isinstance(expense, tuple):
            return expense
        if expense.status == Expense.Status.CANCELED:
            return ServiceResponse.success(data={'expense': cls.serialize(expense)})
        if expense.status not in {Expense.Status.PENDING, Expense.Status.APPROVED}:
            return ServiceResponse.conflict(
                'EXPENSE_CANNOT_BE_CANCELED',
                'Only pending or approved expenses can be canceled.',
                errors={'status': [f'Found {expense.status}.']},
            )
        reason = str(reason or '').strip()
        if expense.status == Expense.Status.PENDING:
            if expense.created_by_id != actor.id:
                return ServiceResponse.forbidden('Only the requester can cancel this request')
        elif not can_approve:
            return ServiceResponse.forbidden('Approver permission is required')
        if expense.status == Expense.Status.APPROVED and not reason:
            return ServiceResponse.validation_error({'reason': ['This field is required.']})
        previous = expense.status
        expense.status = Expense.Status.CANCELED
        expense.canceled_by = actor
        expense.canceled_at = timezone.now()
        expense.cancel_reason = reason
        expense.save(update_fields=[
            'status', 'canceled_by', 'canceled_at', 'cancel_reason', 'updated_at',
        ])
        _transition(
            expense,
            previous,
            Expense.Status.CANCELED,
            actor,
            reason=reason,
        )
        return ServiceResponse.success(data={'expense': cls.serialize(expense)})

    @classmethod
    @transaction.atomic
    def void(
        cls,
        expense_id,
        *,
        actor,
        reason,
        action_id=None,
        idempotency_key='',
    ):
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error({'reason': ['This field is required.']})
        expense = cls._locked(expense_id, actor)
        if isinstance(expense, tuple):
            return expense
        if expense.status == Expense.Status.VOIDED:
            if action_id and expense.void_action_id == action_id:
                return ServiceResponse.success(data={'expense': cls.serialize(expense)})
            return ServiceResponse.conflict(
                'EXPENSE_ALREADY_VOIDED', 'This expense is already voided.',
            )
        if expense.status != Expense.Status.PAID:
            return cls._state_conflict(expense, Expense.Status.PAID)
        reversal_id = None
        cashbox_reversal_id = None
        if expense.treasury_transaction_id:
            from base.services.treasury_service import TreasuryService

            result, status = TreasuryService.reverse_transaction(
                expense.treasury_transaction_id,
                performed_by=actor,
                reason=reason,
                branch_id=expense.branch_id,
                command_id=action_id,
                idempotency_key=idempotency_key,
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            reversal_id = result['data']['transaction']['id']
            expense.treasury_reversal_id = reversal_id
        else:
            cashbox = _cashbox_payment(expense)
            if cashbox is None:
                return ServiceResponse.conflict(
                    'EXPENSE_PAYMENT_LINK_MISSING',
                    'Paid expense has no reversible money posting.',
                )
            from cashbox.services.expense_service import CashboxExpenseService

            result, status = CashboxExpenseService.reverse_payment(
                cashbox,
                actor=actor,
                reason=reason,
                command_id=action_id,
                idempotency_key=idempotency_key,
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            cashbox_reversal_id = result['data']['id']

        expense.status = Expense.Status.VOIDED
        expense.voided_by = actor
        expense.voided_at = timezone.now()
        expense.void_reason = reason
        expense.void_action_id = action_id
        expense.save(update_fields=[
            'status', 'voided_by', 'voided_at', 'void_reason',
            'void_action_id', 'treasury_reversal', 'updated_at',
        ])
        _transition(
            expense,
            Expense.Status.PAID,
            Expense.Status.VOIDED,
            actor,
            reason=reason,
            idempotency_key=idempotency_key,
            metadata={
                'treasury_transaction_id': reversal_id,
                'cashbox_expense_id': cashbox_reversal_id,
            },
        )
        return ServiceResponse.success(data={'expense': cls.serialize(expense)})

    @classmethod
    @transaction.atomic
    def direct_pay(cls, *, actor, action_id, idempotency_key='', **payload):
        if actor is None:
            return ServiceResponse.forbidden('Authenticated staff actor is required')
        User.objects.select_for_update().get(pk=actor.pk)
        existing = (
            Expense.objects.filter(payment_action_id=action_id).first()
            if action_id else None
        )
        if existing:
            return ServiceResponse.success(data=cls._payment_result(existing))
        result, status = cls.create(actor=actor, **payload)
        if status >= 400:
            transaction.set_rollback(True)
            return result, status
        expense_id = result['data']['expense_id']
        result, status = cls.approve(expense_id, actor=actor, direct=True)
        if status >= 400:
            transaction.set_rollback(True)
            return result, status
        return cls.pay(
            expense_id,
            actor=actor,
            source_account=(
                payload.get('source_account') or payload.get('requested_source')
            ),
            fee_uzs=payload.get('fee_uzs', payload.get('fee', 0)),
            fee_percent=payload.get('fee_percent'),
            note=payload.get('note', payload.get('description', '')),
            action_id=action_id,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def mark_paid(cls, expense_id, paid_by_id, payment_method='CASH'):
        actor = User.objects.filter(pk=paid_by_id).first()
        source = (
            Expense.Source.DRAWER
            if str(payment_method).upper() == 'CASH'
            else Expense.Source.BANK
        )
        return cls.pay(expense_id, actor=actor, source_account=source)

    @classmethod
    def get_stats(cls, date_from=None, date_to=None, *, actor=None, branch_id=None):
        result, status = cls.list(
            page=1,
            per_page=1,
            date_from=date_from,
            date_to=date_to,
            actor=actor,
            view_all=True,
            branch_id=branch_id,
        )
        if status >= 400:
            return result, status
        return ServiceResponse.success(data={'stats': result['data']['totals']})

    @classmethod
    @transaction.atomic
    def update(
        cls,
        expense_id,
        *,
        actor=None,
        view_all=False,
        branch_id=None,
        **values,
    ):
        branch_id = str(branch_id or resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403,
            )
        expense = Expense.objects.select_for_update(
            of=('self',),
        ).select_related('category').filter(
            pk=expense_id,
            branch_id=branch_id,
            is_deleted=False,
        ).first()
        if expense is None:
            return ServiceResponse.not_found('Expense not found')
        if actor is not None and not view_all and expense.created_by_id != actor.id:
            return ServiceResponse.forbidden('Only the requester can edit this expense')
        if expense.status != Expense.Status.PENDING:
            return cls._state_conflict(expense, Expense.Status.PENDING)
        allowed = {'description', 'expense_date', 'receipt_number', 'notes'}
        unknown = sorted(set(values) - allowed)
        if unknown:
            return ServiceResponse.validation_error({
                field: ['Unknown field.'] for field in unknown
            })
        if 'expense_date' in values:
            parsed_date = _parse_date(values['expense_date'])
            if parsed_date is None:
                return ServiceResponse.validation_error({
                    'expense_date': ['Use YYYY-MM-DD.'],
                })
            values['expense_date'] = parsed_date
        for field in ('description', 'receipt_number', 'notes'):
            if field in values:
                values[field] = str(values[field] or '').strip()
        category = expense.category
        next_description = values.get('description', expense.description)
        next_receipt = values.get('receipt_number', expense.receipt_number)
        if category and category.requires_description and not next_description:
            return ServiceResponse.validation_error({
                'description': ['Description is required by this category.'],
            })
        if (
            category
            and category.requires_receipt
            and not (next_receipt or expense.receipt_file)
        ):
            return ServiceResponse.validation_error({
                'receipt_number': ['Receipt evidence is required by this category.'],
            })
        changed = []
        for field in allowed:
            if field in values:
                setattr(expense, field, values[field])
                changed.append(field)
        expense.save(update_fields=[*changed, 'updated_at'])
        return ServiceResponse.success(data={'expense': cls.serialize(expense)})

    @classmethod
    @transaction.atomic
    def reclassify_pending(
        cls,
        *,
        expense_ids,
        category_id,
        actor,
        reason='',
        dry_run=True,
        expected_category_id=None,
        action_id=None,
        idempotency_key='',
    ):
        if not isinstance(dry_run, bool):
            return ServiceResponse.validation_error({
                'dry_run': ['Use a JSON boolean.'],
            })
        if not isinstance(expense_ids, list) or not expense_ids:
            return ServiceResponse.validation_error({
                'expense_ids': ['Choose at least one expense.'],
            })
        if len(expense_ids) > 500:
            return ServiceResponse.validation_error({
                'expense_ids': ['At most 500 expenses may be reviewed at once.'],
            })
        if any(
            isinstance(value, bool)
            or not str(value).isascii()
            or not str(value).isdigit()
            or int(value) < 1
            for value in expense_ids
        ):
            return ServiceResponse.validation_error({
                'expense_ids': ['Use positive integer expense IDs.'],
            })
        normalized_ids = [int(value) for value in expense_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            return ServiceResponse.validation_error({
                'expense_ids': ['Duplicate expense IDs are not allowed.'],
            })
        if (
            isinstance(category_id, bool)
            or not str(category_id).isascii()
            or not str(category_id).isdigit()
            or int(category_id) < 1
        ):
            return ServiceResponse.validation_error({
                'category_id': ['Use a positive category ID.'],
            })
        if expected_category_id not in (None, '') and (
            isinstance(expected_category_id, bool)
            or not str(expected_category_id).isascii()
            or not str(expected_category_id).isdigit()
            or int(expected_category_id) < 1
        ):
            return ServiceResponse.validation_error({
                'expected_category_id': ['Use a positive category ID or null.'],
            })
        reason = str(reason or '').strip()
        if not dry_run and not reason:
            return ServiceResponse.validation_error({
                'reason': ['A reason is required to apply reclassification.'],
            })
        if len(reason) > 1000:
            return ServiceResponse.validation_error({
                'reason': ['Maximum length is 1000.'],
            })
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED',
                'Expense branch could not be resolved.',
                403,
            )
        expenses = list(
            Expense.objects.select_for_update(of=('self',)).filter(
                id__in=normalized_ids,
                branch_id=branch_id,
                is_deleted=False,
            ).select_related('category').order_by('id')
        )
        found_ids = {expense.id for expense in expenses}
        missing_ids = sorted(set(normalized_ids) - found_ids)
        if missing_ids:
            return ServiceResponse.failure(
                'EXPENSE_RECLASSIFICATION_SCOPE_MISMATCH',
                'One or more expenses were not found in the authorized branch.',
                404,
                details={'missing_expense_ids': missing_ids},
            )
        invalid_status = [
            expense.id for expense in expenses
            if expense.status != Expense.Status.PENDING
        ]
        if invalid_status:
            return ServiceResponse.conflict(
                'EXPENSE_RECLASSIFICATION_STATE_CONFLICT',
                'Only pending expenses can be reclassified.',
                details={'expense_ids': invalid_status},
            )

        action_token = str(action_id or '').strip()
        if not dry_run and action_token:
            recovery_events = list(ExpenseTransition.objects.filter(
                expense_id__in=normalized_ids,
                branch_id=branch_id,
                actor_id=getattr(actor, 'id', None),
                metadata__event='CATEGORY_RECLASSIFIED',
                metadata__action_id=action_token,
            ).order_by('expense_id', 'id'))
            if recovery_events:
                event_by_expense = {
                    event.expense_id: event for event in recovery_events
                }
                if (
                    len(event_by_expense) != len(expenses)
                    or len(recovery_events) != len(expenses)
                    or any(
                        not isinstance(event.metadata.get('category_before'), dict)
                        or not isinstance(event.metadata.get('category_after'), dict)
                        for event in recovery_events
                    )
                ):
                    return ServiceResponse.conflict(
                        'EXPENSE_RECLASSIFICATION_RECOVERY_CONFLICT',
                        'Reclassification recovery evidence is incomplete.',
                        details={'expense_ids': normalized_ids},
                    )
                before_by_expense = {
                    expense_id: event.metadata.get('category_before')
                    for expense_id, event in event_by_expense.items()
                }
                target_evidence = recovery_events[0].metadata.get('category_after')
                same_target = all(
                    event.metadata.get('category_after', {}).get('category_id')
                    == int(category_id)
                    for event in recovery_events
                )
                if not same_target:
                    return ServiceResponse.conflict(
                        'EXPENSE_RECLASSIFICATION_RECOVERY_CONFLICT',
                        'Idempotent action targets a different category.',
                        details={'expense_ids': normalized_ids},
                    )
                if expected_category_id not in (None, ''):
                    expected_id = int(expected_category_id)
                    if any(
                        evidence.get('category_id') != expected_id
                        for evidence in before_by_expense.values()
                    ):
                        return ServiceResponse.conflict(
                            'EXPENSE_RECLASSIFICATION_RECOVERY_CONFLICT',
                            'Idempotent action has different source evidence.',
                            details={'expense_ids': normalized_ids},
                        )
                response = _reclassification_summary(
                    expenses,
                    normalized_ids,
                    target_evidence,
                    before_by_expense,
                    dry_run=False,
                    mutation_applied=True,
                )
                return ServiceResponse.success(
                    data={'reclassification': response},
                    message='Pending expenses reclassified',
                )

        category = ExpenseCategory.objects.select_for_update(
            of=('self',),
        ).filter(
            pk=int(category_id),
            is_deleted=False,
        ).select_related('parent').first()
        if category is None:
            return ServiceResponse.not_found('Expense category not found')
        if not category.is_active:
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_INACTIVE',
                'Inactive categories cannot be assigned.',
                422,
                errors={'category_id': ['Category is inactive.']},
            )
        if ExpenseCategory.objects.filter(
            parent_id=category.id,
            is_deleted=False,
            is_active=True,
        ).exists():
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_GROUP_ONLY',
                'Choose a subcategory for these expenses.',
                422,
                errors={'category_id': ['This category contains subcategories.']},
            )
        if expected_category_id not in (None, ''):
            expected_category_id = int(expected_category_id)
            changed_elsewhere = [
                expense.id for expense in expenses
                if expense.category_id != expected_category_id
            ]
            if changed_elsewhere:
                return ServiceResponse.conflict(
                    'EXPENSE_RECLASSIFICATION_STALE_SELECTION',
                    'Some expenses no longer have the expected category.',
                    details={'expense_ids': changed_elsewhere},
                )
        unchanged_ids = [
            expense.id for expense in expenses
            if expense.category_id == category.id
        ]
        if unchanged_ids:
            return ServiceResponse.conflict(
                'EXPENSE_RECLASSIFICATION_NO_CHANGE',
                'One or more expenses already use the target category.',
                details={'expense_ids': unchanged_ids},
            )
        allowed_sources = set(
            _category_snapshot(category)['category_allowed_sources_snapshot']
        )
        source_mismatches = [
            expense.id for expense in expenses
            if expense.requested_source not in allowed_sources
        ]
        if source_mismatches:
            return ServiceResponse.failure(
                'EXPENSE_SOURCE_NOT_ALLOWED',
                'The target category does not allow every requested source.',
                422,
                errors={'category_id': ['Payment source is incompatible.']},
                details={'expense_ids': source_mismatches},
            )
        missing_descriptions = [
            expense.id for expense in expenses
            if category.requires_description and not expense.description.strip()
        ]
        missing_receipts = [
            expense.id for expense in expenses
            if category.requires_receipt
            and not (expense.receipt_number.strip() or expense.receipt_file)
        ]
        if missing_descriptions or missing_receipts:
            errors = {}
            if missing_descriptions:
                errors['description'] = ['Description evidence is missing.']
            if missing_receipts:
                errors['receipt_number'] = ['Receipt evidence is missing.']
            return ServiceResponse.failure(
                'EXPENSE_RECLASSIFICATION_EVIDENCE_MISSING',
                'Some expenses do not satisfy the target category rules.',
                422,
                errors=errors,
                details={
                    'missing_description_expense_ids': missing_descriptions,
                    'missing_receipt_expense_ids': missing_receipts,
                },
            )
        before_by_expense = {
            expense.id: _expense_category_evidence(expense)
            for expense in expenses
        }
        target_evidence = _category_assignment_evidence(category)
        response = _reclassification_summary(
            expenses,
            normalized_ids,
            target_evidence,
            before_by_expense,
            dry_run=dry_run,
            mutation_applied=False,
        )
        if dry_run:
            return ServiceResponse.success(data={'reclassification': response})
        snapshot = _category_snapshot(category)
        snapshot_fields = list(snapshot)
        for expense in expenses:
            before = before_by_expense[expense.id]
            for field, value in snapshot.items():
                setattr(expense, field, value)
            expense.save(update_fields=[*snapshot_fields, 'updated_at'])
            _transition(
                expense,
                Expense.Status.PENDING,
                Expense.Status.PENDING,
                actor,
                reason=reason,
                idempotency_key=idempotency_key,
                metadata={
                    'event': 'CATEGORY_RECLASSIFIED',
                    'action_id': action_token or None,
                    'category_before': before,
                    'category_after': _expense_category_evidence(expense),
                },
            )
        response['mutation_applied'] = True
        response['_applied_now'] = True
        return ServiceResponse.success(
            data={'reclassification': response},
            message='Pending expenses reclassified',
        )

    @classmethod
    def delete(cls, expense_id, actor=None):
        return cls.cancel(expense_id, actor=actor, reason='Canceled via legacy route')

    @classmethod
    def _locked(cls, expense_id, actor):
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403,
            )
        expense = Expense.objects.select_for_update(of=('self',)).select_related(
            'category', 'created_by', 'approved_by', 'paid_by',
            'canceled_by', 'voided_by', 'subject_user',
            'treasury_transaction', 'treasury_reversal',
            'cashbox_payment',
        ).filter(
            pk=expense_id,
            branch_id=branch_id,
            is_deleted=False,
        ).first()
        if expense is None:
            return ServiceResponse.not_found('Expense not found')
        return expense

    @classmethod
    def _state_conflict(cls, expense, expected):
        return ServiceResponse.conflict(
            'EXPENSE_STATE_CONFLICT',
            'Expense is not in the required workflow state.',
            errors={'status': [f'Expected {expected} but found {expense.status}.']},
            details={'expense_id': expense.id},
        )

    @classmethod
    def _payment_result(cls, expense):
        cashbox = _cashbox_payment(expense)
        return {
            'expense_id': expense.id,
            'status': expense.status,
            'amount_uzs': uzs_int(expense.amount),
            'fee_uzs': uzs_int(expense.fee_uzs),
            'total_debited_uzs': uzs_int(expense.amount + expense.fee_uzs),
            'source_account': expense.requested_source,
            'treasury_transaction_id': expense.treasury_transaction_id,
            'cashbox_expense_id': cashbox.id if cashbox else None,
            'paid_by': _actor_data(expense.paid_by),
            'paid_at': local_iso(expense.paid_at),
        }
