from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum

from base.helpers.response import ServiceResponse
from hr.models import Expense
from hr.repositories import ExpenseRepository, ExpenseCategoryRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ExpenseService:

    @classmethod
    def serialize(cls, expense: Expense) -> Dict[str, Any]:
        data = {
            "id": expense.id,
            "uuid": str(expense.uuid),
            "category": None,
            "amount": str(expense.amount),
            "description": expense.description,
            "expense_date": expense.expense_date.isoformat(),
            "payment_method": expense.payment_method,
            "payment_method_display": expense.get_payment_method_display(),
            "status": expense.status,
            "status_display": expense.get_status_display(),
            "receipt_number": expense.receipt_number,
            "receipt_image_url": expense.receipt_image_url,
            "created_by_id": expense.created_by_id,
            "created_by": None,
            "approved_by_id": expense.approved_by_id,
            "approved_by": None,
            "paid_by_id": expense.paid_by_id,
            "paid_by": None,
            "notes": expense.notes,
            "created_at": expense.created_at.isoformat(),
            "updated_at": expense.updated_at.isoformat(),
        }

        if expense.category_id and expense.category:
            data["category"] = {
                "id": expense.category.id,
                "name": expense.category.name,
            }

        if expense.created_by_id and expense.created_by:
            data["created_by"] = {
                "id": expense.created_by.id,
                "first_name": expense.created_by.first_name,
                "last_name": expense.created_by.last_name,
            }

        if expense.approved_by_id and expense.approved_by:
            data["approved_by"] = {
                "id": expense.approved_by.id,
                "first_name": expense.approved_by.first_name,
                "last_name": expense.approved_by.last_name,
            }

        if expense.paid_by_id and expense.paid_by:
            data["paid_by"] = {
                "id": expense.paid_by.id,
                "first_name": expense.paid_by.first_name,
                "last_name": expense.paid_by.last_name,
            }

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             status: str = None,
             category_id: int = None,
             date_from=None,
             date_to=None) -> Tuple[Dict[str, Any], int]:
        queryset = Expense.objects.filter(
            is_deleted=False
        ).select_related("category", "created_by", "approved_by", "paid_by")

        if status:
            queryset = queryset.filter(status=status)

        if category_id:
            queryset = queryset.filter(category_id=category_id)

        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)

        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)

        queryset = queryset.order_by("-expense_date", "-created_at")

        page_obj, paginator = ExpenseRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "expenses": [cls.serialize(exp) for exp in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in Expense.Status.choices
            ],
        })

    @classmethod
    def get(cls, expense_id: int) -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_with_relations(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        return ServiceResponse.success(data={
            "expense": cls.serialize(expense),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               amount: Decimal,
               expense_date,
               category_id: int = None,
               description: str = "",
               payment_method: str = "CASH",
               receipt_number: str = "",
               receipt_image_url: str = "",
               created_by_id: int = None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        amount = Decimal(str(amount))
        if amount <= 0:
            return ServiceResponse.validation_error(
                errors={"amount": "Amount must be greater than zero"},
            )

        if category_id:
            category = ExpenseCategoryRepository.get_by_id(category_id)
            if not category:
                return ServiceResponse.not_found("Expense category not found")
            if not category.is_active:
                return ServiceResponse.error("Expense category is inactive")

        expense = ExpenseRepository.create(
            category_id=category_id,
            amount=amount,
            description=description,
            expense_date=expense_date,
            payment_method=payment_method,
            # Expenses start PENDING. The approve/reject endpoints are the
            # real approval gate — self-approval at creation time would let a
            # cashier authorize their own payout.
            status=Expense.Status.PENDING,
            approved_by_id=None,
            receipt_number=receipt_number,
            receipt_image_url=receipt_image_url,
            created_by_id=created_by_id,
            notes=notes,
        )

        expense = ExpenseRepository.get_with_relations(expense.pk)

        return ServiceResponse.created(data={
            "id": expense.id,
            "uuid": str(expense.uuid),
            "expense": cls.serialize(expense),
        }, message="Expense created")

    @classmethod
    @transaction.atomic
    def update(cls, expense_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_with_relations(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        if expense.status == Expense.Status.PAID:
            return ServiceResponse.error(
                "Cannot update a PAID expense"
            )

        if "category_id" in kwargs and kwargs["category_id"]:
            category = ExpenseCategoryRepository.get_by_id(kwargs["category_id"])
            if not category:
                return ServiceResponse.not_found("Expense category not found")

        update_fields = ["updated_at"]
        allowed_fields = [
            "category_id", "amount", "description", "expense_date",
            "payment_method", "receipt_number", "receipt_image_url", "notes",
        ]

        for field in allowed_fields:
            if field in kwargs:
                value = kwargs[field]
                if field == "amount":
                    value = Decimal(str(value))
                setattr(expense, field, value)
                update_fields.append(field)

        expense.save(update_fields=update_fields)

        expense = ExpenseRepository.get_with_relations(expense.pk)

        return ServiceResponse.success(data={
            "expense": cls.serialize(expense),
        }, message="Expense updated")

    @classmethod
    @transaction.atomic
    def delete(cls, expense_id: int) -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_by_id(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        if expense.status == Expense.Status.PAID:
            return ServiceResponse.error(
                "Cannot delete a PAID expense"
            )

        expense.is_deleted = True
        expense.save(update_fields=["is_deleted", "updated_at"])

        return ServiceResponse.success(data={
            "id": expense_id,
        }, message="Expense deleted")

    @classmethod
    @transaction.atomic
    def approve(cls,
                expense_id: int,
                approved_by_id: int) -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_with_relations(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        if expense.status != Expense.Status.PENDING:
            return ServiceResponse.error(
                f"Cannot approve expense in {expense.status} status. Must be PENDING."
            )

        expense.status = Expense.Status.APPROVED
        expense.approved_by_id = approved_by_id
        expense.save(update_fields=["status", "approved_by_id", "updated_at"])

        return ServiceResponse.success(data={
            "expense": cls.serialize(expense),
        }, message="Expense approved")

    @classmethod
    @transaction.atomic
    def reject(cls,
               expense_id: int,
               approved_by_id: int,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_with_relations(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        if expense.status != Expense.Status.PENDING:
            return ServiceResponse.error(
                f"Cannot reject expense in {expense.status} status. Must be PENDING."
            )

        expense.status = Expense.Status.REJECTED
        expense.approved_by_id = approved_by_id
        if notes:
            expense.notes = f"{expense.notes}\nRejected: {notes}".strip()
        expense.save(update_fields=["status", "approved_by_id", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "expense": cls.serialize(expense),
        }, message="Expense rejected")

    @classmethod
    @transaction.atomic
    def mark_paid(cls,
                  expense_id: int,
                  paid_by_id: int,
                  payment_method: str = "CASH") -> Tuple[Dict[str, Any], int]:
        expense = ExpenseRepository.get_with_relations(expense_id)
        if not expense:
            return ServiceResponse.not_found(
                f"Expense with id {expense_id} not found"
            )

        if expense.status != Expense.Status.APPROVED:
            return ServiceResponse.error(
                f"Cannot mark expense as paid in {expense.status} status. Must be APPROVED."
            )

        valid_methods = Expense.PaymentMethod.values
        if payment_method not in valid_methods:
            return ServiceResponse.validation_error(
                errors={"payment_method": f"Must be one of {valid_methods}"},
            )

        from hr.services.cash_transaction_service import CashTransactionService

        result, status = CashTransactionService.create_for_reference(
            type="EXPENSE_PAYMENT",
            amount=expense.amount,
            description=f"Expense #{expense.id}: {expense.description}",
            payment_method=payment_method,
            reference_type="Expense",
            reference_id=expense.id,
            performed_by_id=paid_by_id,
            notes=expense.notes,
        )
        if status >= 400:
            return result, status

        expense.status = Expense.Status.PAID
        expense.paid_by_id = paid_by_id
        expense.payment_method = payment_method
        expense.save(update_fields=[
            "status", "paid_by_id", "payment_method", "updated_at"
        ])

        expense = ExpenseRepository.get_with_relations(expense.pk)

        return ServiceResponse.success(data={
            "expense": cls.serialize(expense),
        }, message="Expense marked as paid")

    @classmethod
    def get_stats(cls,
                  date_from=None,
                  date_to=None) -> Tuple[Dict[str, Any], int]:
        if date_from and date_to:
            stats = ExpenseRepository.get_stats(date_from, date_to)
        else:
            qs = Expense.objects.filter(is_deleted=False)
            by_status = dict(
                qs.values_list("status").annotate(total=Sum("amount"))
            )
            by_category = dict(
                qs.values_list("category__name").annotate(total=Sum("amount"))
            )
            stats = {
                "by_status": by_status,
                "by_category": by_category,
                "total": qs.aggregate(total=Sum("amount"))["total"] or 0,
                "count": qs.count(),
            }

        serialized_stats = {
            "by_status": {k: str(v) for k, v in stats["by_status"].items()},
            "by_category": {k: str(v) for k, v in stats["by_category"].items()},
            "total": str(stats["total"]),
            "count": stats["count"],
        }

        return ServiceResponse.success(data={
            "stats": serialized_stats,
        })
