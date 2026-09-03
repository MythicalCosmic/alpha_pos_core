from collections import defaultdict
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.money import decimal_string, uzs_int
from base.services.branch_scope import resolve_actor_branch
from stock.models import (
    StockBatch,
    StockCategory,
    StockItem,
    StockLocation,
    Supplier,
    SupplierStockItem,
)
from stock.services.supplier_integrity import validate_supplier_ledgers


_QUANTITY_FIELD = DecimalField(max_digits=24, decimal_places=4)


def issue(code, severity, title, message, *, entity_type=None, entity_id=None,
          amount_uzs=None, details=None):
    return {
        'code': code,
        'severity': severity,
        'title': title,
        'message': message,
        'entity_type': entity_type,
        'entity_id': entity_id,
        'amount_uzs': amount_uzs,
        'details': details or {},
    }


def _supplier_snapshot(branch_id):
    payable = Decimal('0')
    credit = Decimal('0')
    balances_safe = True
    issues = []
    suppliers = list(Supplier.objects.filter(
        branch_id=branch_id,
        is_deleted=False,
    ).order_by('id'))
    evidence = validate_supplier_ledgers(suppliers)
    for supplier in suppliers:
        supplier_evidence = evidence[supplier.id]
        if not supplier_evidence.currency_supported:
            balances_safe = False
            issues.append(issue(
                'SUPPLIER_CURRENCY_UNSUPPORTED',
                'ERROR',
                'Supplier currency is not supported',
                'This supplier is excluded from UZS totals until FX is configured.',
                entity_type='Supplier',
                entity_id=supplier.id,
                details={'currency': supplier.currency},
            ))
            continue
        balance = supplier_evidence.stored_balance
        if not supplier_evidence.valid:
            balances_safe = False
            issues.append(issue(
                'SUPPLIER_LEDGER_BALANCE_MISMATCH',
                'ERROR',
                'Supplier balance disagrees with its ledger',
                'The stored supplier balance needs reconciliation.',
                entity_type='Supplier',
                entity_id=supplier.id,
                details={
                    'stored_balance_uzs': str(balance),
                    'ledger_balance_uzs': str(supplier_evidence.ledger_balance),
                },
            ))
            continue
        if balance > 0:
            payable += balance
        elif balance < 0:
            credit += -balance
    return suppliers, evidence, (
        payable if balances_safe else None
    ), (credit if balances_safe else None), issues


def _supplier_totals(branch_id):
    _suppliers, _evidence, payable, credit, issues = _supplier_snapshot(branch_id)
    return payable, credit, issues


class InventoryControlService:
    @classmethod
    def get(cls, *, actor=None, branch_id=None, item_type='RAW',
            location_id=None, category_id=None, include_descendants=False,
            search='', low_stock=None, page=1, per_page=25, as_of=None):
        branch_id = str(branch_id or resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED', 'Inventory branch could not be resolved.', 403,
            )
        item_type = str(item_type or 'RAW').strip().upper()
        if item_type not in StockItem.ItemType.values:
            return ServiceResponse.validation_error({
                'item_type': ['Unknown stock item type.'],
            })
        location = None
        if location_id is not None:
            location = StockLocation.objects.filter(
                pk=location_id,
                branch_id=branch_id,
                is_deleted=False,
                is_active=True,
            ).first()
            if location is None:
                return ServiceResponse.failure(
                    'LOCATION_FORBIDDEN',
                    'Location is outside the authorized branch.',
                    403,
                    details={'location_id': location_id},
                )

        category_ids = None
        if category_id is not None:
            category = StockCategory.objects.filter(
                pk=category_id,
                is_deleted=False,
            ).first()
            if category is None:
                return ServiceResponse.not_found('Stock category not found')
            category_ids = {category.id}
            if include_descendants:
                descendants = list(StockCategory.objects.filter(
                    is_deleted=False,
                ).values_list('id', 'parent_id'))
                changed = True
                while changed:
                    changed = False
                    for child_id, parent_id in descendants:
                        if parent_id in category_ids and child_id not in category_ids:
                            category_ids.add(child_id)
                            changed = True

        level_filter = Q(
            stock_levels__is_deleted=False,
            stock_levels__branch_id=branch_id,
            stock_levels__location__branch_id=branch_id,
            stock_levels__location__is_deleted=False,
        )
        if location:
            level_filter &= Q(stock_levels__location=location)
        queryset = StockItem.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            is_active=True,
            item_type=item_type,
        ).select_related('category', 'base_unit')
        if location:
            queryset = queryset.filter(
                stock_levels__location=location,
                stock_levels__is_deleted=False,
            )
        if category_ids is not None:
            queryset = queryset.filter(category_id__in=category_ids)
        term = str(search or '').strip()
        if term:
            queryset = queryset.filter(
                Q(name__icontains=term)
                | Q(sku__icontains=term)
                | Q(barcode__icontains=term)
            )
        queryset = queryset.annotate(
            control_quantity=Coalesce(
                Sum('stock_levels__quantity', filter=level_filter),
                Value(Decimal('0')),
                output_field=_QUANTITY_FIELD,
            ),
            control_reserved=Coalesce(
                Sum('stock_levels__reserved_quantity', filter=level_filter),
                Value(Decimal('0')),
                output_field=_QUANTITY_FIELD,
            ),
            control_pending_in=Coalesce(
                Sum('stock_levels__pending_in_quantity', filter=level_filter),
                Value(Decimal('0')),
                output_field=_QUANTITY_FIELD,
            ),
            control_pending_out=Coalesce(
                Sum('stock_levels__pending_out_quantity', filter=level_filter),
                Value(Decimal('0')),
                output_field=_QUANTITY_FIELD,
            ),
        ).distinct()
        items = list(queryset)

        batch_queryset = StockBatch.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            stock_item_id__in=[item.id for item in items],
        )
        if location:
            batch_queryset = batch_queryset.filter(location=location)
        batch_totals = {
            row['stock_item_id']: row['total'] or Decimal('0')
            for row in batch_queryset.values('stock_item_id').annotate(
                total=Sum('current_quantity'),
            )
        }
        supplier_links = defaultdict(list)
        unsupported_supplier_links = []
        for link in SupplierStockItem.objects.filter(
            stock_item_id__in=[item.id for item in items],
            is_deleted=False,
            supplier__branch_id=branch_id,
            supplier__is_deleted=False,
        ).select_related('supplier').order_by('id'):
            if link.currency != 'UZS' or link.supplier.currency != 'UZS':
                unsupported_supplier_links.append(link)
            if link.is_preferred:
                supplier_links[link.stock_item_id].append(link)

        suppliers, supplier_evidence, payable, credit, supplier_issues = (
            _supplier_snapshot(branch_id)
        )
        issues = list(supplier_issues)
        issues.extend(issue(
            'SUPPLIER_CURRENCY_UNSUPPORTED',
            'ERROR',
            'Supplier price currency is not supported',
            'The supplier price is excluded from UZS aggregation.',
            entity_type='SupplierStockItem',
            entity_id=link.id,
            details={
                'stock_item_id': link.stock_item_id,
                'price_currency': link.currency,
                'supplier_currency': link.supplier.currency,
            },
        ) for link in unsupported_supplier_links)
        prepared = []
        inventory_total = Decimal('0')
        available_total = Decimal('0')
        inventory_safe = True
        available_safe = True
        for item in items:
            quantity = item.control_quantity or Decimal('0')
            reserved = item.control_reserved or Decimal('0')
            available = quantity - reserved
            cost = item.avg_cost_price or Decimal('0')
            out_of_stock = available <= 0
            is_low = available <= item.reorder_point
            row_inventory_safe = True
            row_available_safe = True
            if quantity < 0:
                row_inventory_safe = False
                row_available_safe = False
                issues.append(issue(
                    'STOCK_LEVEL_NEGATIVE',
                    'ERROR',
                    'Stock level is negative',
                    'A negative on-hand quantity makes valuation unsafe.',
                    entity_type='StockItem',
                    entity_id=item.id,
                    details={
                        'location_id': location.id if location else None,
                        'quantity': decimal_string(quantity),
                    },
                ))
            if reserved < 0 or reserved > quantity:
                row_available_safe = False
                issues.append(issue(
                    'STOCK_RESERVED_EXCEEDS_ON_HAND',
                    'ERROR',
                    'Reserved stock is invalid',
                    'Reserved quantity is negative or exceeds on-hand quantity.',
                    entity_type='StockItem',
                    entity_id=item.id,
                    details={
                        'location_id': location.id if location else None,
                        'quantity': decimal_string(quantity),
                        'reserved_quantity': decimal_string(reserved),
                    },
                ))
            if quantity > 0 and cost <= 0:
                row_inventory_safe = False
                row_available_safe = False
                issues.append(issue(
                    'STOCK_COST_MISSING',
                    'ERROR',
                    'Stock cost is missing',
                    'Positive stock has no usable weighted-average cost.',
                    entity_type='StockItem',
                    entity_id=item.id,
                    details={'quantity': decimal_string(quantity)},
                ))
            if item.track_batches or item.id in batch_totals:
                batch_quantity = batch_totals.get(item.id, Decimal('0'))
                if batch_quantity != quantity:
                    row_inventory_safe = False
                    row_available_safe = False
                    issues.append(issue(
                        'STOCK_LEVEL_BATCH_MISMATCH',
                        'WARNING',
                        'Batch and level quantities differ',
                        'Batch quantities do not reconcile to the stock level.',
                        entity_type='StockItem',
                        entity_id=item.id,
                        details={
                            'location_id': location.id if location else None,
                            'level_quantity': decimal_string(quantity),
                            'batch_quantity': decimal_string(batch_quantity),
                            'difference': decimal_string(quantity - batch_quantity),
                        },
                    ))
            preferred = supplier_links.get(item.id, [])
            if len(preferred) > 1:
                issues.append(issue(
                    'DUPLICATE_PREFERRED_SUPPLIER',
                    'WARNING',
                    'Multiple preferred suppliers are configured',
                    'The lowest stable supplier-link ID is displayed.',
                    entity_type='StockItem',
                    entity_id=item.id,
                    details={'supplier_link_ids': [link.id for link in preferred]},
                ))
            link = preferred[0] if preferred else None
            preferred_data = None
            if link:
                balance_evidence = supplier_evidence.get(link.supplier_id)
                preferred_data = {
                    'supplier_id': link.supplier_id,
                    'supplier_name': link.supplier.name,
                    'price': decimal_string(link.price),
                    'currency': link.currency,
                    'current_balance_uzs': (
                        uzs_int(balance_evidence.stored_balance)
                        if balance_evidence
                        and balance_evidence.currency_supported
                        and balance_evidence.valid
                        else None
                    ),
                    'lead_time_days': (
                        link.lead_time_days
                        if link.lead_time_days is not None
                        else link.supplier.lead_time_days
                    ),
                }
            inventory_value = quantity * cost
            available_value = available * cost
            if row_inventory_safe:
                inventory_total += inventory_value
            else:
                inventory_safe = False
            if row_available_safe:
                available_total += available_value
            else:
                available_safe = False
            prepared.append({
                '_out': out_of_stock,
                '_low': is_low,
                '_name': item.name.casefold(),
                '_id': item.id,
                'stock_item': {
                    'id': item.id,
                    'name': item.name,
                    'code': item.sku,
                },
                'category': ({
                    'id': item.category_id,
                    'name': item.category.name,
                } if item.category_id else None),
                'base_unit': {
                    'id': item.base_unit_id,
                    'name': item.base_unit.name,
                    'code': item.base_unit.short_name,
                },
                'location': ({
                    'id': location.id,
                    'name': location.name,
                } if location else None),
                'quantity': decimal_string(quantity),
                'reserved_quantity': decimal_string(reserved),
                'available_quantity': decimal_string(available),
                'pending_in_quantity': decimal_string(item.control_pending_in),
                'pending_out_quantity': decimal_string(item.control_pending_out),
                'avg_cost_uzs': decimal_string(cost),
                'inventory_value_uzs': (
                    uzs_int(inventory_value) if row_inventory_safe else None
                ),
                'available_value_uzs': (
                    uzs_int(available_value) if row_available_safe else None
                ),
                'reorder_point': decimal_string(item.reorder_point),
                'reorder_threshold_source': 'ITEM',
                'is_low_stock': is_low,
                'is_out_of_stock': out_of_stock,
                'preferred_supplier': preferred_data,
            })
        if low_stock is not None:
            prepared = [
                row for row in prepared
                if (row['_low'] if low_stock else not row['_low'])
            ]
            visible_ids = {row['_id'] for row in prepared}
            issues = [
                row for row in issues
                if row['entity_type'] != 'StockItem'
                or row['entity_id'] in visible_ids
            ]
            issues = [
                row for row in issues
                if row['entity_type'] != 'SupplierStockItem'
                or (row.get('details') or {}).get('stock_item_id') in visible_ids
            ]
            inventory_total = sum(
                Decimal(str(row['quantity'])) * Decimal(str(row['avg_cost_uzs']))
                for row in prepared if row['inventory_value_uzs'] is not None
            )
            available_total = sum(
                Decimal(str(row['available_quantity']))
                * Decimal(str(row['avg_cost_uzs']))
                for row in prepared if row['available_value_uzs'] is not None
            )
            inventory_safe = all(
                row['inventory_value_uzs'] is not None for row in prepared
            )
            available_safe = all(
                row['available_value_uzs'] is not None for row in prepared
            )

        prepared.sort(key=lambda row: (
            not row['_out'],
            not row['_low'],
            row['_name'],
            row['_id'],
        ))
        total = len(prepared)
        low_count = sum(row['_low'] for row in prepared)
        out_count = sum(row['_out'] for row in prepared)
        page_rows = prepared[(page - 1) * per_page:page * per_page]
        for row in page_rows:
            for key in ('_out', '_low', '_name', '_id'):
                row.pop(key, None)
        unsafe = any(row['severity'] == 'ERROR' for row in issues)
        completeness = 'UNSAFE' if unsafe else ('PARTIAL' if issues else 'COMPLETE')
        as_of = as_of or timezone.now()
        return ServiceResponse.success(data={
            'summary': {
                'inventory_value_uzs': (
                    uzs_int(inventory_total) if inventory_safe else None
                ),
                'available_value_uzs': (
                    uzs_int(available_total) if available_safe else None
                ),
                'raw_item_count': total,
                'low_stock_count': low_count,
                'out_of_stock_count': out_count,
                'supplier_payable_uzs': (
                    uzs_int(payable) if payable is not None else None
                ),
                'supplier_credit_uzs': (
                    uzs_int(credit) if credit is not None else None
                ),
                'valuation_method': 'WEIGHTED_AVERAGE',
                'as_of': timezone.localtime(as_of).isoformat(),
            },
            'completeness': {'status': completeness, 'issues': issues},
            'issues': issues,
            'items': page_rows,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': (total + per_page - 1) // per_page,
            },
        })
