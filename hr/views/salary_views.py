from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import SalaryService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def salaries(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = SalaryService.list(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = SalaryService.create(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def salary_detail(request, salary_id):
    if request.method == "GET":
        result, status = SalaryService.get(salary_id)
        return JsonResponse(result, status=status)

    if request.method == "DELETE":
        result, status = SalaryService.delete(salary_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = SalaryService.update(salary_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_generate(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = SalaryService.generate_payroll(
        year=data["year"],
        month=data["month"],
        created_by_id=request.user.id,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_approve(request, salary_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = SalaryService.approve(salary_id, approved_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_approve_all(request):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = SalaryService.approve_all(
        year=data["year"],
        month=data["month"],
        approved_by_id=request.user.id,
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_pay(request, salary_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # Honor the client-supplied payment_method (CASH/BANK/CARD/etc.).
    # The previous default of CASH caused bank-paid salaries to wrongly
    # debit the cash drawer, leaving the register short by the salary
    # amount even though no cash had been disbursed.
    kwargs = {"paid_by_id": request.user.id}
    if data and "payment_method" in data:
        kwargs["payment_method"] = data["payment_method"]
    result, status = SalaryService.pay(salary_id, **kwargs)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def salary_summary(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    if not year or not month:
        from django.utils import timezone
        now = timezone.now()
        year = year or now.year
        month = month or now.month
    result, status = SalaryService.get_payroll_summary(
        year=int(year),
        month=int(month),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def salary_bonuses(request, salary_id):
    from hr.services.salary_item_service import SalaryItemService
    if request.method == "GET":
        result, status = SalaryItemService.items(salary_id)
        return JsonResponse(result, status=status)
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = SalaryItemService.add_bonus(
        salary_id, amount=data.get("amount"), reason=data.get("reason", ""))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_deductions(request, salary_id):
    from hr.services.salary_item_service import SalaryItemService
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = SalaryItemService.add_deduction(
        salary_id, amount=data.get("amount"), reason=data.get("reason", ""))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def salary_set_base(request, salary_id):
    from hr.services.salary_item_service import SalaryItemService
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    result, status = SalaryItemService.set_base(salary_id, amount=data.get("amount"))
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["DELETE"])
@admin_required
def salary_bonus_delete(request, salary_id, bonus_id):
    from hr.services.salary_item_service import SalaryItemService
    result, status = SalaryItemService.remove_bonus(salary_id, bonus_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["DELETE"])
@admin_required
def salary_deduction_delete(request, salary_id, deduction_id):
    from hr.services.salary_item_service import SalaryItemService
    result, status = SalaryItemService.remove_deduction(salary_id, deduction_id)
    return JsonResponse(result, status=status)
