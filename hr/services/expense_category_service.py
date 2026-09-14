import re

from django.db import transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from base.financial import EXPENSE_REPORTING_GROUPS, FinancialReportingGroup
from base.helpers.response import ServiceResponse
from base.money import MoneyValueError, local_iso, whole_uzs
from base.services.branch_scope import resolve_actor_branch
from hr.models import Expense, ExpenseCategory


_SOURCES = {'DRAWER', 'SAFE', 'BANK'}
_CODE_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]{1,63}$')
_MUTABLE_FIELDS = {
    'name', 'description', 'budget_limit', 'reporting_group', 'is_active',
    'sort_order', 'allowed_sources', 'requires_receipt',
    'requires_description', 'parent_id', 'cost_behavior',
}


def _generated_code(name):
    base = re.sub(r'[^A-Z0-9]+', '_', str(name or '').upper()).strip('_')
    base = (base or 'EXPENSE')[:56]
    candidate = base
    suffix = 2
    while ExpenseCategory.objects.filter(code=candidate).exists():
        candidate = f'{base[:56]}_{suffix}'
        suffix += 1
    return candidate


def _actor_name(actor):
    if actor is None:
        return None
    return {
        'id': actor.id,
        'name': f'{actor.first_name} {actor.last_name}'.strip(),
    }


class ExpenseCategoryService:
    @classmethod
    def _queryset(cls, branch_id=None):
        direct_expenses = Expense.objects.filter(
            category_id=OuterRef('pk'),
            is_deleted=False,
        )
        descendant_expenses = Expense.objects.filter(
            category__parent_id=OuterRef('pk'),
            category__is_deleted=False,
            is_deleted=False,
        )
        if branch_id:
            direct_expenses = direct_expenses.filter(branch_id=branch_id)
            descendant_expenses = descendant_expenses.filter(
                branch_id=branch_id,
            )
        direct_expenses = direct_expenses.values('category_id').annotate(
            total=Count('id'),
        ).values('total')
        descendant_expenses = descendant_expenses.values(
            'category__parent_id',
        ).annotate(
            total=Count('id'),
        ).values('total')
        children = ExpenseCategory.objects.filter(
            parent_id=OuterRef('pk'),
            is_deleted=False,
        ).values('parent_id').annotate(
            total=Count('id'),
        ).values('total')
        active_children = ExpenseCategory.objects.filter(
            parent_id=OuterRef('pk'),
            is_deleted=False,
            is_active=True,
        ).values('parent_id').annotate(
            total=Count('id'),
        ).values('total')
        return ExpenseCategory.objects.filter(is_deleted=False).select_related(
            'parent', 'created_by', 'updated_by',
        ).annotate(
            direct_expense_count=Coalesce(
                Subquery(direct_expenses, output_field=IntegerField()),
                Value(0),
            ),
            descendant_expense_count=Coalesce(
                Subquery(descendant_expenses, output_field=IntegerField()),
                Value(0),
            ),
            child_count=Coalesce(
                Subquery(children, output_field=IntegerField()),
                Value(0),
            ),
            active_child_count=Coalesce(
                Subquery(active_children, output_field=IntegerField()),
                Value(0),
            ),
        )

    @classmethod
    def serialize(cls, category):
        direct_expense_count = getattr(category, 'direct_expense_count', 0)
        descendant_expense_count = getattr(
            category, 'descendant_expense_count', 0,
        )
        child_count = getattr(category, 'child_count', 0)
        active_child_count = getattr(category, 'active_child_count', 0)
        parent = None
        if category.parent_id:
            parent = {
                'id': category.parent_id,
                'uuid': str(category.parent.uuid),
                'code': category.parent.code,
                'name': category.parent.name,
                'is_active': category.parent.is_active,
            }
        return {
            'id': category.id,
            'uuid': str(category.uuid),
            'code': category.code,
            'name': category.name,
            'description': category.description,
            'parent_id': category.parent_id,
            'parent': parent,
            'depth': 1 if category.parent_id else 0,
            'path': [
                *([category.parent.name] if category.parent_id else []),
                category.name,
            ],
            'cost_behavior': category.cost_behavior,
            'budget_limit': (
                str(category.budget_limit)
                if category.budget_limit is not None else None
            ),
            'reporting_group': category.reporting_group,
            'is_active': category.is_active,
            'sort_order': category.sort_order,
            'allowed_sources': category.allowed_sources,
            'requires_receipt': category.requires_receipt,
            'requires_description': category.requires_description,
            'expense_count': direct_expense_count,
            'direct_expense_count': direct_expense_count,
            'descendant_expense_count': descendant_expense_count,
            'subtree_expense_count': (
                direct_expense_count + descendant_expense_count
            ),
            'child_count': child_count,
            'active_child_count': active_child_count,
            'is_selectable': category.is_active and active_child_count == 0,
            'created_by': _actor_name(category.created_by),
            'updated_by': _actor_name(category.updated_by),
            'created_at': local_iso(category.created_at),
            'updated_at': local_iso(category.updated_at),
        }

    @classmethod
    def list(
        cls,
        page=1,
        per_page=100,
        search=None,
        is_active=True,
        include_inactive=False,
        parent_id=None,
        cost_behavior=None,
        roots_only=False,
        actor=None,
        branch_id=None,
    ):
        if not isinstance(roots_only, bool):
            return ServiceResponse.validation_error({
                'roots_only': ['Use a JSON boolean.'],
            })
        if roots_only and parent_id is not None:
            return ServiceResponse.validation_error({
                'parent_id': ['Cannot be combined with roots_only=true.'],
            })
        if parent_id is not None and (
            isinstance(parent_id, bool)
            or not str(parent_id).isascii()
            or not str(parent_id).isdigit()
            or int(parent_id) < 1
        ):
            return ServiceResponse.validation_error({
                'parent_id': ['Use a positive category ID.'],
            })
        if parent_id is not None:
            parent_id = int(parent_id)
        branch_id = str(
            branch_id or resolve_actor_branch(actor) or '',
        ).strip()
        queryset = cls._queryset(branch_id or None)
        if include_inactive:
            is_active = None
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        if roots_only:
            queryset = queryset.filter(parent__isnull=True)
        elif parent_id is not None:
            queryset = queryset.filter(parent_id=parent_id)
        if cost_behavior:
            normalized_behavior = str(cost_behavior).strip().upper()
            if normalized_behavior not in ExpenseCategory.CostBehavior.values:
                return ServiceResponse.validation_error({
                    'cost_behavior': ['Unknown cost behavior.'],
                })
            queryset = queryset.filter(cost_behavior=normalized_behavior)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(description__icontains=search)
                | Q(parent__name__icontains=search)
                | Q(parent__code__icontains=search)
            )
        queryset = queryset.annotate(
            tree_sort=Case(
                When(parent__isnull=True, then=F('sort_order')),
                default=F('parent__sort_order'),
                output_field=IntegerField(),
            ),
            tree_name=Case(
                When(parent__isnull=True, then=F('name')),
                default=F('parent__name'),
                output_field=CharField(),
            ),
            depth_sort=Case(
                When(parent__isnull=True, then=0),
                default=1,
                output_field=IntegerField(),
            ),
        ).order_by(
            'tree_sort',
            'tree_name',
            'depth_sort',
            'sort_order',
            'name',
            'id',
        )
        total = queryset.count()
        rows = queryset[(page - 1) * per_page:page * per_page]
        total_pages = (total + per_page - 1) // per_page
        return ServiceResponse.success(data={
            'categories': [cls.serialize(category) for category in rows],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': total_pages,
            },
        })

    @classmethod
    def get(cls, category_id, *, actor=None, branch_id=None):
        branch_id = str(
            branch_id or resolve_actor_branch(actor) or '',
        ).strip()
        category = cls._queryset(branch_id or None).filter(pk=category_id).first()
        if category is None:
            return ServiceResponse.not_found('Expense category not found')
        return ServiceResponse.success(data={'category': cls.serialize(category)})

    @classmethod
    @transaction.atomic
    def create(
        cls,
        name=None,
        code=None,
        description='',
        budget_limit=None,
        is_active=True,
        reporting_group=FinancialReportingGroup.REVIEW,
        parent_id=None,
        cost_behavior=ExpenseCategory.CostBehavior.UNCLASSIFIED,
        sort_order=0,
        allowed_sources=None,
        requires_receipt=False,
        requires_description=False,
        actor=None,
        **extra,
    ):
        if extra:
            return ServiceResponse.validation_error({
                field: ['Unknown field.'] for field in sorted(extra)
            })
        name = str(name or '').strip()
        if not name:
            return ServiceResponse.validation_error({'name': ['This field is required.']})
        if len(name) > 100:
            return ServiceResponse.validation_error({'name': ['Maximum length is 100.']})
        code = str(code or _generated_code(name)).strip().upper()
        if not _CODE_PATTERN.fullmatch(code):
            return ServiceResponse.validation_error({
                'code': ['Use 2-64 uppercase letters, numbers, or underscores.'],
            })
        if ExpenseCategory.objects.filter(code=code).exists():
            return ServiceResponse.validation_error({'code': ['Code already exists.']})
        reporting_group = str(reporting_group or '').strip().upper()
        if reporting_group not in EXPENSE_REPORTING_GROUPS:
            return ServiceResponse.validation_error({
                'reporting_group': ['Unknown financial reporting group.'],
            })
        cost_behavior = str(cost_behavior or '').strip().upper()
        if cost_behavior not in ExpenseCategory.CostBehavior.values:
            return ServiceResponse.validation_error({
                'cost_behavior': ['Unknown cost behavior.'],
            })
        sources, source_error = cls._validate_sources(
            allowed_sources if allowed_sources is not None else sorted(_SOURCES)
        )
        if source_error:
            return source_error
        cleaned, error = cls._validate_configuration(
            budget_limit=budget_limit,
            sort_order=sort_order,
            is_active=is_active,
            requires_receipt=requires_receipt,
            requires_description=requires_description,
        )
        if error:
            return error
        parent, parent_error = cls._resolve_parent(
            parent_id,
            is_active=cleaned['is_active'],
            lock=True,
        )
        if parent_error:
            return parent_error
        category = ExpenseCategory.objects.create(
            code=code,
            name=name,
            description=str(description or '').strip(),
            parent=parent,
            cost_behavior=cost_behavior,
            budget_limit=cleaned['budget_limit'],
            reporting_group=reporting_group,
            is_active=cleaned['is_active'],
            sort_order=cleaned['sort_order'],
            allowed_sources=sources,
            requires_receipt=cleaned['requires_receipt'],
            requires_description=cleaned['requires_description'],
            created_by=actor,
            updated_by=actor,
        )
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        category = cls._queryset(branch_id or None).get(pk=category.pk)
        return ServiceResponse.created(data={
            'category': cls.serialize(category),
        }, message='Expense category created')

    @classmethod
    @transaction.atomic
    def update(cls, category_id, actor=None, **values):
        category = ExpenseCategory.objects.select_for_update(
            of=('self',),
        ).filter(
            pk=category_id,
            is_deleted=False,
        ).select_related('parent').first()
        if category is None:
            return ServiceResponse.not_found('Expense category not found')
        if 'code' in values and str(values['code']).upper() != category.code:
            return ServiceResponse.validation_error({
                'code': ['Code is immutable.'],
            })
        unknown = set(values) - _MUTABLE_FIELDS - {'code'}
        if unknown:
            return ServiceResponse.validation_error({
                field: ['Unknown field.'] for field in sorted(unknown)
            })
        if 'reporting_group' in values:
            values['reporting_group'] = str(
                values['reporting_group'] or '',
            ).strip().upper()
            if values['reporting_group'] not in EXPENSE_REPORTING_GROUPS:
                return ServiceResponse.validation_error({
                    'reporting_group': ['Unknown financial reporting group.'],
                })
        if 'cost_behavior' in values:
            values['cost_behavior'] = str(
                values['cost_behavior'] or '',
            ).strip().upper()
            if values['cost_behavior'] not in ExpenseCategory.CostBehavior.values:
                return ServiceResponse.validation_error({
                    'cost_behavior': ['Unknown cost behavior.'],
                })
        if 'allowed_sources' in values:
            sources, source_error = cls._validate_sources(values['allowed_sources'])
            if source_error:
                return source_error
            values['allowed_sources'] = sources
        configuration = {
            field: values[field]
            for field in (
                'budget_limit', 'sort_order', 'is_active',
                'requires_receipt', 'requires_description',
            )
            if field in values
        }
        cleaned, error = cls._validate_configuration(**configuration)
        if error:
            return error
        values.update(cleaned)
        next_active = values.get('is_active', category.is_active)
        if 'parent_id' in values:
            parent, parent_error = cls._resolve_parent(
                values.pop('parent_id'),
                category=category,
                is_active=next_active,
                lock=True,
            )
            if parent_error:
                return parent_error
            values['parent'] = parent
        elif category.parent_id and next_active and not category.parent.is_active:
            return ServiceResponse.failure(
                'EXPENSE_CATEGORY_PARENT_INACTIVE',
                'An active category requires an active parent.',
                422,
                errors={'parent_id': ['Parent category is inactive.']},
            )
        if not next_active and ExpenseCategory.objects.filter(
            parent_id=category.id,
            is_deleted=False,
            is_active=True,
        ).exists():
            return ServiceResponse.conflict(
                'EXPENSE_CATEGORY_ACTIVE_CHILDREN',
                'Deactivate active subcategories first.',
                errors={'is_active': ['Active subcategories still exist.']},
            )
        fields = [
            'name', 'description', 'parent', 'cost_behavior',
            'budget_limit', 'reporting_group',
            'is_active', 'sort_order', 'allowed_sources',
            'requires_receipt', 'requires_description',
        ]
        changed = []
        for field in fields:
            if field not in values:
                continue
            value = values[field]
            if field in {'name', 'description'}:
                value = str(value or '').strip()
            if field == 'name' and not value:
                return ServiceResponse.validation_error({
                    'name': ['This field is required.'],
                })
            if field == 'name' and len(value) > 100:
                return ServiceResponse.validation_error({
                    'name': ['Maximum length is 100.'],
                })
            setattr(category, field, value)
            changed.append(field)
        category.updated_by = actor
        category.save(update_fields=[*changed, 'updated_by', 'updated_at'])
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        category = cls._queryset(branch_id or None).get(pk=category.pk)
        return ServiceResponse.success(data={
            'category': cls.serialize(category),
        }, message='Expense category updated')

    @classmethod
    @transaction.atomic
    def deactivate(cls, category_id, actor=None):
        category = ExpenseCategory.objects.select_for_update().filter(
            pk=category_id,
            is_deleted=False,
        ).first()
        if category is None:
            return ServiceResponse.not_found('Expense category not found')
        if category.is_active:
            if ExpenseCategory.objects.filter(
                parent_id=category.id,
                is_deleted=False,
                is_active=True,
            ).exists():
                return ServiceResponse.conflict(
                    'EXPENSE_CATEGORY_ACTIVE_CHILDREN',
                    'Deactivate active subcategories first.',
                    errors={'is_active': ['Active subcategories still exist.']},
                )
            category.is_active = False
            category.updated_by = actor
            category.save(update_fields=['is_active', 'updated_by', 'updated_at'])
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        category = cls._queryset(branch_id or None).get(pk=category.pk)
        return ServiceResponse.success(data={
            'category': cls.serialize(category),
        }, message='Expense category is inactive')

    delete = deactivate

    @staticmethod
    def _resolve_parent(parent_id, *, category=None, is_active=True, lock=False):
        if parent_id in (None, ''):
            return None, None
        if (
            isinstance(parent_id, bool)
            or not str(parent_id).isascii()
            or not str(parent_id).isdigit()
            or int(parent_id) < 1
        ):
            return None, ServiceResponse.validation_error({
                'parent_id': ['Use a positive category ID or null.'],
            })
        parent_id = int(parent_id)
        if category is not None and parent_id == category.id:
            return None, ServiceResponse.validation_error({
                'parent_id': ['A category cannot be its own parent.'],
            })
        queryset = ExpenseCategory.objects.filter(
            pk=parent_id,
            is_deleted=False,
        )
        if lock:
            queryset = queryset.select_for_update(of=('self',))
        parent = queryset.first()
        if parent is None:
            return None, ServiceResponse.failure(
                'EXPENSE_CATEGORY_PARENT_NOT_FOUND',
                'Parent category not found.',
                422,
                errors={'parent_id': ['Parent category not found.']},
            )
        if parent.parent_id:
            return None, ServiceResponse.failure(
                'EXPENSE_CATEGORY_DEPTH_EXCEEDED',
                'Only one subcategory level is supported.',
                422,
                errors={'parent_id': ['Choose a root category.']},
            )
        if is_active and not parent.is_active:
            return None, ServiceResponse.failure(
                'EXPENSE_CATEGORY_PARENT_INACTIVE',
                'An active category requires an active parent.',
                422,
                errors={'parent_id': ['Parent category is inactive.']},
            )
        if category is not None and ExpenseCategory.objects.filter(
            parent_id=category.id,
            is_deleted=False,
        ).exists():
            return None, ServiceResponse.failure(
                'EXPENSE_CATEGORY_DEPTH_EXCEEDED',
                'A category with subcategories cannot become a subcategory.',
                422,
                errors={'parent_id': ['This category already has subcategories.']},
            )
        return parent, None

    @staticmethod
    def _validate_sources(sources):
        if not isinstance(sources, list) or not sources:
            return None, ServiceResponse.validation_error({
                'allowed_sources': ['Choose at least one source.'],
            })
        normalized = [str(source).strip().upper() for source in sources]
        if len(set(normalized)) != len(normalized) or not set(normalized) <= _SOURCES:
            return None, ServiceResponse.validation_error({
                'allowed_sources': ['Allowed values are DRAWER, SAFE, and BANK.'],
            })
        return normalized, None

    @staticmethod
    def _validate_configuration(**values):
        cleaned = {}
        for field in ('is_active', 'requires_receipt', 'requires_description'):
            if field not in values:
                continue
            value = values[field]
            if not isinstance(value, bool):
                return None, ServiceResponse.validation_error({
                    field: ['Use a JSON boolean.'],
                })
            cleaned[field] = value
        if 'sort_order' in values:
            value = values['sort_order']
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
                return None, ServiceResponse.validation_error({
                    'sort_order': ['Use a non-negative integer.'],
                })
            value = int(value)
            if value > 2147483647:
                return None, ServiceResponse.validation_error({
                    'sort_order': ['Value is too large.'],
                })
            cleaned['sort_order'] = value
        if 'budget_limit' in values:
            value = values['budget_limit']
            if value in (None, ''):
                cleaned['budget_limit'] = None
            else:
                try:
                    cleaned['budget_limit'] = whole_uzs(
                        value,
                        'budget_limit',
                        maximum='9999999999',
                    )
                except MoneyValueError as exc:
                    return None, ServiceResponse.validation_error({
                        'budget_limit': [str(exc)],
                    })
        return cleaned, None
