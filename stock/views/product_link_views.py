from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import ProductStockLinkService


@csrf_exempt
@require_GET
@admin_required
def product_links(request):
    result, status = ProductStockLinkService.list(
        page=safe_page(request),
        per_page=safe_per_page(request, 50),
        link_type=request.GET.get("type"),
        active_only=request.GET.get("active", "true").lower() == "true",
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def product_link_detail(request, link_id):
    if request.method == "GET":
        result, status = ProductStockLinkService.get(link_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        link = ProductStockLinkService.get_by_id(link_id)
        if link:
            result, status = ProductStockLinkService.unlink(link.product_id)
            return JsonResponse(result, status=status)
        return JsonResponse(
            {"success": False, "message": f"Product link {link_id} not found"},
            status=404,
        )

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductStockLinkService.update(link_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def product_link_by_product(request, product_id):
    result, status = ProductStockLinkService.get_by_product(product_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def product_link_to_recipe(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductStockLinkService.link_to_recipe(
        product_id=product_id,
        recipe_id=data["recipe_id"],
        deduct_on_status=data.get("deduct_on_status", "PREPARING"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def product_link_to_item(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductStockLinkService.link_to_item(
        product_id=product_id,
        stock_item_id=data["stock_item_id"],
        quantity_per_sale=data.get("quantity_per_sale", 1),
        unit_id=data.get("unit_id"),
        deduct_on_status=data.get("deduct_on_status", "PREPARING"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def product_link_with_components(request, product_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ProductStockLinkService.link_with_components(
        product_id=product_id,
        components=data["components"],
        deduct_on_status=data.get("deduct_on_status", "PREPARING"),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["DELETE"])
@admin_required
def product_unlink(request, product_id):
    result, status = ProductStockLinkService.unlink(product_id)
    return JsonResponse(result, status=status)
