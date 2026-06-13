from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction
from django.db.models import Count, Q
from django.core.paginator import Paginator

from base.helpers.response import ServiceResponse
from hr.models import ExpenseCategory
from hr.repositories import ExpenseCategoryRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ExpenseCategoryService:

    @classmethod
    def serialize(cls, category: ExpenseCategory) -> Dict[str, Any]:
        return {
            "id": category.id,
            "uuid": str(category.uuid),
            "name": category.name,
            "description": category.description,
            "budget_limit": str(category.budget_limit) if category.budget_limit is not None else None,
            "is_active": category.is_active,
            "expense_count": getattr(category, "expense_count", 0),
            "created_at": category.created_at.isoformat(),
            "updated_at": category.updated_at.isoformat(),
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             is_active: bool = None) -> Tuple[Dict[str, Any], int]:
        queryset = ExpenseCategoryRepository.get_all()

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        queryset = queryset.annotate(
            expense_count=Count("expenses", filter=Q(expenses__is_deleted=False))
        )

        queryset = queryset.order_by("name")

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "categories": [cls.serialize(cat) for cat in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
        })

    @classmethod
    def get(cls, category_id: int) -> Tuple[Dict[str, Any], int]:
        category = ExpenseCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(
                f"Expense category with id {category_id} not found"
            )

        category = ExpenseCategory.objects.filter(
            pk=category.pk, is_deleted=False
        ).annotate(
            expense_count=Count("expenses", filter=Q(expenses__is_deleted=False))
        ).first()

        return ServiceResponse.success(data={
            "category": cls.serialize(category),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               description: str = "",
               budget_limit=None,
               is_active: bool = True) -> Tuple[Dict[str, Any], int]:
        if ExpenseCategoryRepository.name_exists(name):
            return ServiceResponse.validation_error(
                errors={"name": f"Expense category '{name}' already exists"},
            )

        kwargs = {
            "name": name,
            "description": description,
            "is_active": is_active,
        }
        if budget_limit is not None:
            kwargs["budget_limit"] = Decimal(str(budget_limit))

        category = ExpenseCategoryRepository.create(**kwargs)

        return ServiceResponse.created(data={
            "id": category.id,
            "uuid": str(category.uuid),
            "category": cls.serialize(category),
        }, message=f"Expense category '{name}' created")

    @classmethod
    @transaction.atomic
    def update(cls, category_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        category = ExpenseCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(
                f"Expense category with id {category_id} not found"
            )

        if "name" in kwargs and kwargs["name"] != category.name:
            if ExpenseCategoryRepository.name_exists(kwargs["name"], exclude_id=category_id):
                return ServiceResponse.validation_error(
                    errors={"name": f"Expense category '{kwargs['name']}' already exists"},
                )

        update_fields = ["updated_at"]
        for field in ["name", "description", "is_active"]:
            if field in kwargs:
                setattr(category, field, kwargs[field])
                update_fields.append(field)

        if "budget_limit" in kwargs:
            value = kwargs["budget_limit"]
            category.budget_limit = Decimal(str(value)) if value is not None else None
            update_fields.append("budget_limit")

        category.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "category": cls.serialize(category),
        }, message="Expense category updated")

    @classmethod
    @transaction.atomic
    def delete(cls, category_id: int) -> Tuple[Dict[str, Any], int]:
        category = ExpenseCategoryRepository.get_by_id(category_id)
        if not category:
            return ServiceResponse.not_found(
                f"Expense category with id {category_id} not found"
            )

        pending_expenses = category.expenses.filter(
            is_deleted=False, status="PENDING"
        ).count()
        if pending_expenses > 0:
            return ServiceResponse.error(
                f"Cannot delete category with {pending_expenses} pending expense(s). "
                "Resolve or reassign them first."
            )

        category.is_active = False
        category.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": category_id,
        }, message="Expense category deactivated")
