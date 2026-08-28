from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import (
    StockAdjustmentRequest, StockItem, StockLocation, StockTransaction, StockUnit,
)


def _serialize(row):
    return {
        'id': row.id,
        'stock_item_id': row.stock_item_id,
        'stock_item_name': row.stock_item.name,
        'location_id': row.location_id,
        'location_name': row.location.name,
        'unit_id': row.unit_id,
        'quantity': str(row.quantity),
        'reason': row.reason,
        'evidence': row.evidence,
        'status': row.status,
        'requested_by': row.requested_by_id,
        'requested_at': row.requested_at.isoformat(),
        'reviewed_by': row.reviewed_by_id,
        'reviewed_at': row.reviewed_at.isoformat() if row.reviewed_at else None,
        'review_note': row.review_note,
        'stock_transaction_id': row.stock_transaction_id,
    }


class StockAdjustmentRequestService:
    @staticmethod
    def list(*, actor, page=1, per_page=20, status=None, view_all=False):
        qs = StockAdjustmentRequest.objects.filter(is_deleted=False).select_related(
            'stock_item', 'location', 'unit', 'requested_by', 'reviewed_by',
        )
        if not view_all:
            qs = qs.filter(requested_by=actor)
        if status:
            qs = qs.filter(status=status)
        paginator = Paginator(qs.order_by('-requested_at'), per_page)
        page_obj = paginator.get_page(page)
        return ServiceResponse.success(data={
            'adjustment_requests': [_serialize(row) for row in page_obj],
            'pagination': {
                'page': page_obj.number, 'per_page': per_page,
                'total': paginator.count, 'total_pages': paginator.num_pages,
            },
        })

    @staticmethod
    @transaction.atomic
    def create(*, stock_item_id, location_id, unit_id, quantity, reason,
               evidence, requested_by):
        try:
            qty = Decimal(str(quantity))
        except (InvalidOperation, TypeError, ValueError):
            return ServiceResponse.validation_error(errors={'quantity': 'Invalid quantity'})
        errors = {}
        if qty == 0:
            errors['quantity'] = 'Must not be zero'
        if not str(reason or '').strip():
            errors['reason'] = 'Required'
        if not str(evidence or '').strip():
            errors['evidence'] = 'Required'
        item = StockItem.objects.filter(id=stock_item_id, is_deleted=False, is_active=True).first()
        location = StockLocation.objects.filter(id=location_id, is_deleted=False, is_active=True).first()
        unit = StockUnit.objects.filter(id=unit_id, is_deleted=False, is_active=True).first()
        if not item:
            errors['stock_item_id'] = 'Not found'
        if not location:
            errors['location_id'] = 'Not found'
        if not unit:
            errors['unit_id'] = 'Not found'
        if errors:
            return ServiceResponse.validation_error(errors=errors)
        row = StockAdjustmentRequest.objects.create(
            stock_item=item, location=location, unit=unit, quantity=qty,
            reason=str(reason).strip(), evidence=str(evidence).strip(),
            requested_by=requested_by, branch_id=location.branch_id,
        )
        row = StockAdjustmentRequest.objects.select_related(
            'stock_item', 'location', 'unit', 'requested_by',
        ).get(pk=row.pk)
        return ServiceResponse.created(data={'adjustment_request': _serialize(row)})

    @staticmethod
    @transaction.atomic
    def review(request_id, *, reviewer, approve, note):
        row = StockAdjustmentRequest.objects.select_for_update().select_related(
            'stock_item', 'location', 'unit', 'requested_by',
        ).filter(id=request_id, is_deleted=False).first()
        if not row:
            return ServiceResponse.not_found('Adjustment request not found')
        if row.status != StockAdjustmentRequest.Status.PENDING:
            return ({'success': False, 'message': 'Adjustment request already reviewed',
                     'errors': {'code': 'adjustment_already_reviewed'}}, 409)
        if row.requested_by_id == reviewer.id:
            return ({'success': False, 'message': 'You cannot approve your own adjustment',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        if not str(note or '').strip():
            return ServiceResponse.validation_error(errors={'review_note': 'Required'})
        row.reviewed_by = reviewer
        row.reviewed_at = timezone.now()
        row.review_note = str(note).strip()
        if approve:
            from stock.services.level_service import StockLevelService
            result, status = StockLevelService.adjust(
                stock_item_id=row.stock_item_id, location_id=row.location_id,
                quantity=abs(row.quantity),
                movement_type=('ADJUSTMENT_PLUS' if row.quantity > 0 else 'ADJUSTMENT_MINUS'),
                user_id=reviewer.id,
                unit_id=row.unit_id, reference_type='StockAdjustmentRequest',
                reference_id=row.id, notes=f'{row.reason}: {row.evidence}',
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            transaction_id = (result.get('data') or {}).get('transaction_id')
            if transaction_id:
                row.stock_transaction = StockTransaction.objects.get(id=transaction_id)
            row.status = StockAdjustmentRequest.Status.APPROVED
        else:
            row.status = StockAdjustmentRequest.Status.REJECTED
        row.save(update_fields=[
            'status', 'reviewed_by', 'reviewed_at', 'review_note',
            'stock_transaction', 'updated_at',
        ])
        return ServiceResponse.success(data={'adjustment_request': _serialize(row)})
