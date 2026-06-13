from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page, safe_int
from base.helpers.response import json_response
from base.security.permissions import admin_required
from stock.services import RecipeService, RecipeIngredientService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def recipes(request):
    if request.method == "GET":
        result, status = RecipeService.list(
            page=safe_page(request),
            per_page=safe_per_page(request, 20),
            search=request.GET.get("search"),
            recipe_type=request.GET.get("recipe_type"),
            output_item_id=safe_int(request, "output_item_id"),
            active_only=request.GET.get("active_only", "true").lower() == "true",
            active_version_only=request.GET.get("active_version_only", "true").lower() == "true",
            production_location_id=safe_int(request, "production_location_id"),
        )
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = RecipeService.create(
        **{k: v for k, v in data.items() if k != "created_by_id"},
        created_by_id=request.user.id,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def recipe_detail(request, recipe_id):
    if request.method == "GET":
        include_cost = request.GET.get("include_cost", "false").lower() == "true"
        result, status = RecipeService.get(recipe_id, include_cost=include_cost)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = RecipeService.deactivate(recipe_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = RecipeService.update(recipe_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def recipe_cost(request, recipe_id):
    from decimal import Decimal

    batch_multiplier = Decimal(request.GET.get("batch_multiplier", "1"))
    total_cost = RecipeService.calculate_cost(recipe_id, batch_multiplier)
    return JsonResponse(
        {"success": True, "data": {"total_cost": str(total_cost)}},
        status=200,
    )


@csrf_exempt
@require_GET
@admin_required
def recipe_availability(request, recipe_id):
    from decimal import Decimal

    batch_multiplier = Decimal(request.GET.get("batch_multiplier", "1"))
    result, status = RecipeService.check_availability(recipe_id, batch_multiplier=batch_multiplier)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def recipe_ingredients(request, recipe_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = RecipeIngredientService.add(recipe_id=recipe_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["PUT", "DELETE"])
@admin_required
def recipe_ingredient_detail(request, ingredient_id):
    if request.method == "DELETE":
        result, status = RecipeIngredientService.remove(ingredient_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = RecipeIngredientService.update(ingredient_id, **data)
    return JsonResponse(result, status=status)
