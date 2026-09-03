from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_int
from base.helpers.response import json_response
from base.security.permissions import (
    backoffice_required, permission_denied_response, user_has_permission,
)
from base.services.branch_scope import resolve_actor_branch
from stock.services import StockLocationService


def _branch(request):
    branch_id = str(resolve_actor_branch(request.user) or '').strip()
    if branch_id:
        return branch_id, None
    return None, JsonResponse({
        'success': False,
        'code': 'BRANCH_SCOPE_REQUIRED',
        'message': 'Stock location branch could not be resolved.',
    }, status=403)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@backoffice_required
def locations(request):
    branch_id, branch_error = _branch(request)
    if branch_error:
        return branch_error
    if request.method == "GET":
        if not (
            user_has_permission(request.user, 'stock.level.view')
            or user_has_permission(request.user, 'stock.inventory_control.view')
        ):
            return permission_denied_response(request, 'stock.level.view')
        location_type = request.GET.get("type")
        parent_id = safe_int(request, "parent_id")
        tree = request.GET.get("tree", "false").lower() == "true"
        include_inactive = request.GET.get("include_inactive", "false").lower() == "true"

        if tree:
            result, status = StockLocationService.get_tree(
                include_inactive=include_inactive,
                branch_id=branch_id,
            )
        else:
            result, status = StockLocationService.list(
                include_inactive=include_inactive,
                type_filter=location_type,
                parent_id=parent_id,
                branch_id=branch_id,
            )
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    data.pop('branch_id', None)

    result, status = StockLocationService.create(**data, branch_id=branch_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@backoffice_required
def location_detail(request, location_id):
    branch_id, branch_error = _branch(request)
    if branch_error:
        return branch_error
    if request.method == "GET":
        if not (
            user_has_permission(request.user, 'stock.level.view')
            or user_has_permission(request.user, 'stock.inventory_control.view')
        ):
            return permission_denied_response(request, 'stock.level.view')
        result, status = StockLocationService.get(location_id, branch_id=branch_id)
        return JsonResponse(result, status=status)

    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    if request.method == "DELETE":
        result, status = StockLocationService.deactivate(location_id, branch_id=branch_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    data.pop('branch_id', None)

    result, status = StockLocationService.update(
        location_id, branch_id=branch_id, **data,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def location_set_default(request, location_id):
    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    branch_id, branch_error = _branch(request)
    if branch_error:
        return branch_error
    result, status = StockLocationService.set_default(location_id, branch_id=branch_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@backoffice_required
def location_activate(request, location_id):
    if denied := permission_denied_response(request, 'stock.manage'):
        return denied
    branch_id, branch_error = _branch(request)
    if branch_error:
        return branch_error
    result, status = StockLocationService.activate(location_id, branch_id=branch_id)
    return JsonResponse(result, status=status)
