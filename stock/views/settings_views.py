from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import StockSettingsService, AlertConfigService


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def settings(request):
    if request.method == "GET":
        result, status = StockSettingsService.get_all()
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = StockSettingsService.update(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def settings_toggle(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    module = data.get("module", "stock")
    enabled = data.get("enabled", True)

    if module == "stock":
        result, status = StockSettingsService.toggle_stock(enabled)
    else:
        result, status = StockSettingsService.toggle_module(module, enabled)

    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def alerts(request):
    if request.method == "GET":
        result, status = AlertConfigService.get_all()
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    alert_type = data["alert_type"]
    rest = {k: v for k, v in data.items() if k != "alert_type"}
    result, status = AlertConfigService.create_or_update(alert_type=alert_type, **rest)
    return JsonResponse(result, status=status)
