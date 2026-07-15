from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockLocationService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def locations(request):
    if request.method == "GET":
        location_type = request.GET.get("type")
        parent_id = safe_int(request, "parent_id")
        tree = request.GET.get("tree", "false").lower() == "true"
        include_inactive = request.GET.get("include_inactive", "false").lower() == "true"

        if tree:
            result, status = StockLocationService.get_tree(
                include_inactive=include_inactive,
            )
        else:
            result, status = StockLocationService.list(
                include_inactive=include_inactive,
                type_filter=location_type,
                parent_id=parent_id,
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


@csrf_exempt
@require_POST
@admin_required
def location_activate(request, location_id):
    result, status = StockLocationService.activate(location_id)
    return JsonResponse(result, status=status)
