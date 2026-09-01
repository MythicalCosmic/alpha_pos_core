import re

from django.db import transaction
from django.db.models import Count, Q

from base.financial import EXPENSE_REPORTING_GROUPS, FinancialReportingGroup
from base.helpers.response import ServiceResponse
from base.money import MoneyValueError, local_iso, whole_uzs
from hr.models import ExpenseCategory


_SOURCES = {'DRAWER', 'SAFE', 'BANK'}
_CODE_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]{1,63}$')
_MUTABLE_FIELDS = {
    'name', 'description', 'budget_limit', 'reporting_group', 'is_active',
    'sort_order', 'allowed_sources', 'requires_receipt',
    'requires_description',
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
    def serialize(cls, category):
        return {
            'id': category.id,
            'uuid': str(category.uuid),
            'code': category.code,
            'name': category.name,
            'description': category.description,
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
            'expense_count': getattr(category, 'expense_count', 0),
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
    ):
        queryset = ExpenseCategory.objects.filter(is_deleted=False).select_related(
            'created_by', 'updated_by',
        ).annotate(
            expense_count=Count(
                'expenses',
                filter=Q(expenses__is_deleted=False),
            ),
        )
        if include_inactive:
            is_active = None
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(description__icontains=search)
            )
        queryset = queryset.order_by('sort_order', 'name', 'id')
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
    def get(cls, category_id):
        category = ExpenseCategory.objects.filter(
            pk=category_id,
            is_deleted=False,
        ).select_related('created_by', 'updated_by').annotate(
            expense_count=Count(
                'expenses', filter=Q(expenses__is_deleted=False),
            ),
        ).first()
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
        category = ExpenseCategory.objects.create(
            code=code,
            name=name,
            description=str(description or '').strip(),
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
        return ServiceResponse.created(data={
            'category': cls.serialize(category),
        }, message='Expense category created')

    @classmethod
    @transaction.atomic
    def update(cls, category_id, actor=None, **values):
        category = ExpenseCategory.objects.select_for_update().filter(
            pk=category_id,
            is_deleted=False,
        ).first()
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
            if values['reporting_group'] not in EXPENSE_REPORTING_GROUPS:
                return ServiceResponse.validation_error({
                    'reporting_group': ['Unknown financial reporting group.'],
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
        fields = [
            'name', 'description', 'budget_limit', 'reporting_group',
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
            category.is_active = False
            category.updated_by = actor
            category.save(update_fields=['is_active', 'updated_by', 'updated_at'])
        return ServiceResponse.success(data={
            'category': cls.serialize(category),
        }, message='Expense category is inactive')

    delete = deactivate

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
