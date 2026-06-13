from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required, pos_staff_required
from hr.services import ExpenseCategoryService, ExpenseService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@pos_staff_required
def expense_categories(request):
    if request.method == "GET":
        # Cashiers need to read categories to file an expense.
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = ExpenseCategoryService.list(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    # Creating categories stays a manager/admin job.
    if request.user.role not in ('ADMIN', 'MANAGER'):
        return JsonResponse(
            {"success": False, "message": "Manager access required"}, status=403)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseCategoryService.create(**data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def expense_category_detail(request, category_id):
    if request.method == "GET":
        result, status = ExpenseCategoryService.get(category_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = ExpenseCategoryService.delete(category_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseCategoryService.update(category_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@pos_staff_required
def expenses(request):
    # Cashiers, managers and admins can file (POST) and view (GET) expenses.
    # Created expenses are PENDING; approving/paying them stays admin-only below.
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = ExpenseService.list(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseService.create(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@pos_staff_required
def expense_detail(request, expense_id):
    if request.method == "GET":
        # Cashiers can view an expense they filed.
        result, status = ExpenseService.get(expense_id)
        return JsonResponse(result, status=status)

    # Editing/deleting an expense stays a manager/admin job.
    if request.user.role not in ('ADMIN', 'MANAGER'):
        return JsonResponse(
            {"success": False, "message": "Manager access required"}, status=403)

    if request.method == "DELETE":
        result, status = ExpenseService.delete(expense_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseService.update(expense_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def expense_approve(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseService.approve(expense_id, approved_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def expense_reject(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ExpenseService.reject(
        expense_id, approved_by_id=request.user.id, notes=data.get("notes", ""))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def expense_pay(request, expense_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # Honor the client-supplied payment_method (CASH/UZCARD/HUMO/etc.).
    # The previous default of CASH caused bank/card-paid expenses to wrongly
    # debit the cash drawer, leaving the register short by the expense amount
    # even though no cash had been disbursed.
    kwargs = {"paid_by_id": request.user.id}
    if data and "payment_method" in data:
        kwargs["payment_method"] = data["payment_method"]
    result, status = ExpenseService.mark_paid(expense_id, **kwargs)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@pos_staff_required
def expense_stats(request):
    # Read-only expense totals — visible to cashiers too.
    result, status = ExpenseService.get_stats()
    return JsonResponse(result, status=status)
