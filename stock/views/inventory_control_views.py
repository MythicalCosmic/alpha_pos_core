from django.http import JsonResponse
from django.views.decorators.http import require_GET

from base.http_validation import (
    QueryValidationError,
    boolean,
    optional_int,
    positive_int,
)
from base.security.permissions import backoffice_permission_required
from stock.services.inventory_control_service import InventoryControlService


@require_GET
@backoffice_permission_required('stock.inventory_control.view')
def inventory_control(request):
    try:
        location_id = optional_int(request.GET, 'location_id')
        category_id = optional_int(request.GET, 'category_id')
        include_descendants = boolean(
            request.GET,
            'include_descendants',
            False,
        )
        low_stock = boolean(request.GET, 'low_stock')
        page = positive_int(request.GET, 'page', 1)
        per_page = positive_int(request.GET, 'per_page', 25, maximum=100)
    except QueryValidationError as exc:
        return JsonResponse({
            'success': False,
            'code': 'FILTER_VALIDATION_ERROR',
            'message': 'One or more filters are invalid.',
            'errors': exc.errors,
        }, status=422)
    result, status = InventoryControlService.get(
        actor=request.user,
        item_type=request.GET.get('item_type', 'RAW'),
        location_id=location_id,
        category_id=category_id,
        include_descendants=include_descendants,
        search=request.GET.get('search', ''),
        low_stock=low_stock,
        page=page,
        per_page=per_page,
    )
    return JsonResponse(result, status=status)
