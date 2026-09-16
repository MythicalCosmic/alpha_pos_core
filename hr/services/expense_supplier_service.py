"""Attribute supplier purchases recorded as expenses to their suppliers.

A linked expense keeps its amount, status and Safe/Bank/Drawer movement, so
profit and balances do not change. It is listed on the supplier page as a paid
purchase and the Expenses page can leave it out.
"""
from django.db import transaction
from django.db.models import Count, Q, Sum

from base.financial import FinancialReportingGroup
from base.helpers.response import ServiceResponse
from base.money import local_iso, uzs_int
from base.services.branch_scope import resolve_actor_branch
from hr.models import Expense, ExpenseSupplierLink

MAX_IDS = 500
LINKABLE_STATUSES = (Expense.Status.PENDING, Expense.Status.APPROVED, Expense.Status.PAID)


def _ids(value):
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_IDS
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value)
        or len(set(value)) != len(value)
    ):
        return None
    return value


def supplier_data(expense):
    link = getattr(expense, 'supplier_link', None)
    if link is None:
        return None
    return {'id': link.supplier_id, 'name': link.supplier.name}


class ExpenseSupplierService:

    @classmethod
    @transaction.atomic
    def link(cls, *, expense_ids, supplier_id, actor, note='', dry_run=False):
        from stock.models import Supplier

        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure('BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403)
        ids = _ids(expense_ids)
        if ids is None:
            return ServiceResponse.validation_error({
                'expense_ids': [f'Provide 1-{MAX_IDS} unique positive expense IDs.'],
            })
        if not isinstance(dry_run, bool):
            return ServiceResponse.validation_error({'dry_run': ['Use a JSON boolean.']})
        supplier = Supplier.objects.filter(pk=supplier_id, branch_id=branch_id, is_deleted=False).first() \
            if isinstance(supplier_id, int) and not isinstance(supplier_id, bool) else None
        if supplier is None:
            return ServiceResponse.not_found('Supplier not found')

        expenses = list(
            Expense.objects.select_for_update(of=('self',))
            .filter(pk__in=ids, branch_id=branch_id, is_deleted=False)
            .order_by('id')
        )
        missing = sorted(set(ids) - {expense.id for expense in expenses})
        if missing:
            return ServiceResponse.failure(
                'EXPENSE_NOT_FOUND', 'One or more expenses were not found.', 404,
                details={'expense_ids': missing},
            )
        not_purchases = [
            expense.id for expense in expenses
            if expense.status not in LINKABLE_STATUSES
            or expense.category_reporting_group_snapshot != FinancialReportingGroup.INVENTORY_PURCHASE
        ]
        if not_purchases:
            return ServiceResponse.failure(
                'EXPENSE_NOT_A_SUPPLIER_PURCHASE',
                'Only active supplier-purchase expenses can be linked to a supplier.',
                422,
                details={'expense_ids': not_purchases},
            )

        existing = {
            link.expense_id: link
            for link in ExpenseSupplierLink.objects.select_for_update().filter(expense_id__in=ids)
        }
        moved = [eid for eid, link in existing.items() if link.supplier_id != supplier.id]
        summary = {
            'dry_run': dry_run,
            'supplier': {'id': supplier.id, 'name': supplier.name},
            'expense_count': len(expenses),
            'amount_uzs': uzs_int(sum(expense.amount for expense in expenses)),
            'newly_linked': len(ids) - len(existing),
            'moved_from_other_supplier': moved,
        }
        if dry_run:
            return ServiceResponse.success(data={'link': summary})

        note = str(note or '').strip()[:1000]
        for expense in expenses:
            link = existing.get(expense.id)
            if link is None:
                ExpenseSupplierLink.objects.create(
                    expense=expense, supplier=supplier, branch_id=branch_id,
                    note=note, linked_by=actor,
                )
            elif link.supplier_id != supplier.id or (note and link.note != note):
                link.supplier = supplier
                link.note = note or link.note
                link.linked_by = actor
                link.save(update_fields=['supplier', 'note', 'linked_by', 'updated_at'])
        return ServiceResponse.success(data={'link': summary}, message='Expenses linked to the supplier')

    @classmethod
    @transaction.atomic
    def unlink(cls, *, expense_ids, actor):
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure('BRANCH_SCOPE_REQUIRED', 'Expense branch could not be resolved.', 403)
        ids = _ids(expense_ids)
        if ids is None:
            return ServiceResponse.validation_error({
                'expense_ids': [f'Provide 1-{MAX_IDS} unique positive expense IDs.'],
            })
        deleted, _ = ExpenseSupplierLink.objects.filter(expense_id__in=ids, branch_id=branch_id).delete()
        return ServiceResponse.success(data={'unlinked': deleted}, message='Supplier links removed')

    @classmethod
    def purchases(cls, supplier_id, *, actor, date_from=None, date_to=None, page=1, per_page=25):
        from stock.models import Supplier

        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure('BRANCH_SCOPE_REQUIRED', 'Supplier branch could not be resolved.', 403)
        if not Supplier.objects.filter(pk=supplier_id, branch_id=branch_id, is_deleted=False).exists():
            return ServiceResponse.not_found('Supplier not found')

        queryset = Expense.objects.filter(
            supplier_link__supplier_id=supplier_id,
            branch_id=branch_id,
            is_deleted=False,
            status__in=LINKABLE_STATUSES,
        )
        if date_from:
            queryset = queryset.filter(expense_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(expense_date__lte=date_to)
        totals = queryset.aggregate(
            row_count=Count('id'),
            total_amount=Sum('amount'),
            paid_amount=Sum('amount', filter=Q(status=Expense.Status.PAID)),
        )
        by_source = {
            row['requested_source']: {'count': row['count'], 'amount_uzs': uzs_int(row['source_amount'])}
            for row in queryset.values('requested_source').annotate(count=Count('id'), source_amount=Sum('amount'))
        }
        total = totals['row_count'] or 0
        rows = queryset.order_by('-expense_date', '-id')[(page - 1) * per_page:page * per_page]
        return ServiceResponse.success(data={
            'purchases': [{
                'expense_id': expense.id,
                'date': expense.expense_date.isoformat(),
                'amount_uzs': uzs_int(expense.amount),
                'description': expense.description,
                'category': expense.category_name_snapshot or None,
                'source_account': expense.requested_source or None,
                'status': expense.status,
                'paid_at': local_iso(expense.paid_at),
            } for expense in rows],
            'totals': {
                'count': total,
                'amount_uzs': uzs_int(totals['total_amount'] or 0),
                'paid_uzs': uzs_int(totals['paid_amount'] or 0),
                'by_source': by_source,
            },
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            },
        })
