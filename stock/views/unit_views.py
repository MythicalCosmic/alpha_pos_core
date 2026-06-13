from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockUnitService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def units(request):
    if request.method == "GET":
        unit_type = request.GET.get("type")
        if unit_type:
            result, status = StockUnitService.get_by_type(unit_type)
        else:
            result, status = StockUnitService.list()
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockUnitService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def unit_detail(request, unit_id):
    if request.method == "GET":
        result, status = StockUnitService.get(unit_id)
        return JsonResponse(result, status=status)

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
@admin_required
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
