from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response
from base.security.permissions import admin_required
from hr.services import ReviewService


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def reviews(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = ReviewService.list_reviews(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # reviewer_id must be the authenticated user — ignore any value supplied
    # in the request body to prevent posting reviews on behalf of others.
    result, status = ReviewService.create_review(
        employee_id=data.get("employee_id"),
        reviewer_id=request.user.id,
        period_start=data.get("period_start"),
        period_end=data.get("period_end"),
        rating=data.get("rating", 3),
        strengths=data.get("strengths", ""),
        improvements=data.get("improvements", ""),
        goals=data.get("goals", ""),
    )
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def review_detail(request, review_id):
    if request.method == "GET":
        result, status = ReviewService.get_review(review_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    # Strip identity/audit fields from mass-assigned payload — only the
    # editable review content (rating, strengths, improvements, goals) and
    # period dates are user-controllable.
    for protected in ("reviewer_id", "employee_id", "created_at",
                      "updated_at", "id", "uuid"):
        data.pop(protected, None)

    result, status = ReviewService.update_review(review_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def review_submit(request, review_id):
    result, status = ReviewService.submit_review(review_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def review_acknowledge(request, review_id):
    result, status = ReviewService.acknowledge_review(review_id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def goals(request):
    if request.method == "GET":
        page = safe_page(request)
        per_page = safe_per_page(request, 20)
        result, status = ReviewService.list_goals(page=page, per_page=per_page)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ReviewService.create_goal(**data, created_by_id=request.user.id)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def goal_detail(request, goal_id):
    if request.method == "GET":
        result, status = ReviewService.get_goal(goal_id)
        return JsonResponse(result, status=status)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    for protected in ("created_by_id", "employee_id", "created_at",
                      "updated_at", "id", "uuid"):
        data.pop(protected, None)

    result, status = ReviewService.update_goal(goal_id, **data)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_POST
@admin_required
def goal_progress(request, goal_id):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    result, status = ReviewService.update_progress(
        goal_id,
        progress_percent=data.get("progress_percent"),
        status=data.get("status"),
    )
    return JsonResponse(result, status=status)
