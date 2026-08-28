from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import (
    backoffice_required, backoffice_permission_required, permission_denied_response,
)
from stock.services import StockUnitService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def units(request):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.catalog.view'):
            return denied
        unit_type = request.GET.get("type")
        if unit_type:
            result, status = StockUnitService.get_by_type(unit_type)
        else:
            result, status = StockUnitService.list()
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # Older service versions do not expose activation at creation time.  The
    # desktop sends the form's is_active field unconditionally, so discard it
    # instead of leaking a TypeError/500; new units safely start active.
    data.pop("is_active", None)
    result, status = StockUnitService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@backoffice_required
def unit_detail(request, unit_id):
    if request.method == "GET":
        if denied := permission_denied_response(request, 'stock.catalog.view'):
            return denied
        result, status = StockUnitService.get(unit_id)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    if request.method == "DELETE":
        result, status = StockUnitService.deactivate(unit_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockUnitService.update(unit_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_permission_required('stock.catalog.view')
def unit_convert(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, details = StockUnitService.convert(
        quantity=data["quantity"],
        from_unit_id=data["from_unit_id"],
        to_unit_id=data["to_unit_id"],
    )
    return JsonResponse({"result": str(result), "details": details})
