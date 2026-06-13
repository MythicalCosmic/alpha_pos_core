from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockLocationService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def locations(request):
    if request.method == "GET":
        location_type = request.GET.get("type")
        parent_id = request.GET.get("parent_id")
        tree = request.GET.get("tree", "false").lower() == "true"

        if tree:
            result, status = StockLocationService.get_tree()
        else:
            result, status = StockLocationService.list(
                type_filter=location_type,
                parent_id=int(parent_id) if parent_id else None,
            )
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockLocationService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def location_detail(request, location_id):
    if request.method == "GET":
        result, status = StockLocationService.get(location_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = StockLocationService.deactivate(location_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockLocationService.update(location_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def location_set_default(request, location_id):
    result, status = StockLocationService.set_default(location_id)
    return JsonResponse(result, status=status)
