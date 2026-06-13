from typing import Dict, Any, Tuple
from django.db import transaction
from django.core.paginator import Paginator
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import PerformanceReview, PerformanceGoal
from hr.repositories import EmployeeRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ReviewService:

    # ── Serializers ───────────────────────────────────────────────

    @classmethod
    def _serialize_review(cls, review: PerformanceReview) -> Dict[str, Any]:
        data = {
            "id": review.id,
            "uuid": str(review.uuid),
            "employee": None,
            "reviewer": None,
            "review_period_start": review.review_period_start.isoformat(),
            "review_period_end": review.review_period_end.isoformat(),
            "rating": review.rating,
            "strengths": review.strengths,
            "improvements": review.improvements,
            "goals": review.goals,
            "status": review.status,
            "status_display": review.get_status_display(),
            "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
            "acknowledged_at": review.acknowledged_at.isoformat() if review.acknowledged_at else None,
            "created_at": review.created_at.isoformat(),
            "updated_at": review.updated_at.isoformat(),
        }

        if review.employee_id and review.employee:
            emp = review.employee
            emp_data = {"id": emp.id, "position": emp.position}
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        if review.reviewer_id and review.reviewer:
            data["reviewer"] = {
                "id": review.reviewer.id,
                "first_name": review.reviewer.first_name,
                "last_name": review.reviewer.last_name,
            }

        return data

    @classmethod
    def _serialize_goal(cls, goal: PerformanceGoal) -> Dict[str, Any]:
        data = {
            "id": goal.id,
            "uuid": str(goal.uuid),
            "employee": None,
            "title": goal.title,
            "description": goal.description,
            "target_date": goal.target_date.isoformat() if goal.target_date else None,
            "status": goal.status,
            "status_display": goal.get_status_display(),
            "progress_percent": goal.progress_percent,
            "created_by": None,
            "notes": goal.notes,
            "created_at": goal.created_at.isoformat(),
            "updated_at": goal.updated_at.isoformat(),
        }

        if goal.employee_id and goal.employee:
            emp = goal.employee
            emp_data = {"id": emp.id, "position": emp.position}
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        if goal.created_by_id and goal.created_by:
            data["created_by"] = {
                "id": goal.created_by.id,
                "first_name": goal.created_by.first_name,
                "last_name": goal.created_by.last_name,
            }

        return data

    # ── Reviews ───────────────────────────────────────────────────

    @classmethod
    def list_reviews(cls,
                     page: int = 1,
                     per_page: int = 20,
                     employee_id: int = None,
                     status: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = PerformanceReview.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'reviewer')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if status:
            queryset = queryset.filter(status=status)

        queryset = queryset.order_by('-review_period_end')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "reviews": [cls._serialize_review(r) for r in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in PerformanceReview.Status.choices
            ],
        })

    @classmethod
    def get_review(cls, review_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            review = PerformanceReview.objects.select_related(
                'employee__user', 'reviewer'
            ).get(pk=review_id, is_deleted=False)
        except PerformanceReview.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance review with id {review_id} not found"
            )

        return ServiceResponse.success(data={
            "review": cls._serialize_review(review),
        })

    @classmethod
    @transaction.atomic
    def create_review(cls,
                      employee_id: int,
                      reviewer_id: int,
                      period_start=None,
                      period_end=None,
                      rating: int = 3,
                      strengths: str = "",
                      improvements: str = "",
                      goals: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        from base.models import User
        if not User.objects.filter(pk=reviewer_id, is_deleted=False).exists():
            return ServiceResponse.not_found("Reviewer not found")

        review = PerformanceReview.objects.create(
            employee_id=employee_id,
            reviewer_id=reviewer_id,
            review_period_start=period_start,
            review_period_end=period_end,
            rating=rating,
            strengths=strengths,
            improvements=improvements,
            goals=goals,
            status=PerformanceReview.Status.DRAFT,
        )

        review = PerformanceReview.objects.select_related(
            'employee__user', 'reviewer'
        ).get(pk=review.pk)

        return ServiceResponse.created(data={
            "review": cls._serialize_review(review),
        }, message="Performance review created")

    @classmethod
    @transaction.atomic
    def update_review(cls, review_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        try:
            review = PerformanceReview.objects.select_related(
                'employee__user', 'reviewer'
            ).get(pk=review_id, is_deleted=False)
        except PerformanceReview.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance review with id {review_id} not found"
            )

        if review.status != PerformanceReview.Status.DRAFT:
            return ServiceResponse.error(
                "Can only update reviews in DRAFT status"
            )

        allowed_fields = [
            "review_period_start", "review_period_end", "rating",
            "strengths", "improvements", "goals",
        ]

        update_fields = ["updated_at"]
        for field in allowed_fields:
            if field in kwargs:
                setattr(review, field, kwargs[field])
                update_fields.append(field)

        review.save(update_fields=update_fields)

        review = PerformanceReview.objects.select_related(
            'employee__user', 'reviewer'
        ).get(pk=review.pk)

        return ServiceResponse.success(data={
            "review": cls._serialize_review(review),
        }, message="Performance review updated")

    @classmethod
    @transaction.atomic
    def submit_review(cls, review_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            review = PerformanceReview.objects.select_related(
                'employee__user', 'reviewer'
            ).get(pk=review_id, is_deleted=False)
        except PerformanceReview.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance review with id {review_id} not found"
            )

        if review.status != PerformanceReview.Status.DRAFT:
            return ServiceResponse.error(
                "Can only submit reviews in DRAFT status"
            )

        review.status = PerformanceReview.Status.SUBMITTED
        review.submitted_at = timezone.now()
        review.save(update_fields=["status", "submitted_at", "updated_at"])

        review = PerformanceReview.objects.select_related(
            'employee__user', 'reviewer'
        ).get(pk=review.pk)

        return ServiceResponse.success(data={
            "review": cls._serialize_review(review),
        }, message="Performance review submitted")

    @classmethod
    @transaction.atomic
    def acknowledge_review(cls, review_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            review = PerformanceReview.objects.select_related(
                'employee__user', 'reviewer'
            ).get(pk=review_id, is_deleted=False)
        except PerformanceReview.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance review with id {review_id} not found"
            )

        if review.status != PerformanceReview.Status.SUBMITTED:
            return ServiceResponse.error(
                "Can only acknowledge reviews in SUBMITTED status"
            )

        review.status = PerformanceReview.Status.ACKNOWLEDGED
        review.acknowledged_at = timezone.now()
        review.save(update_fields=["status", "acknowledged_at", "updated_at"])

        review = PerformanceReview.objects.select_related(
            'employee__user', 'reviewer'
        ).get(pk=review.pk)

        return ServiceResponse.success(data={
            "review": cls._serialize_review(review),
        }, message="Performance review acknowledged")

    # ── Goals ─────────────────────────────────────────────────────

    @classmethod
    def list_goals(cls,
                   page: int = 1,
                   per_page: int = 20,
                   employee_id: int = None,
                   status: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = PerformanceGoal.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'created_by')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if status:
            queryset = queryset.filter(status=status)

        queryset = queryset.order_by('-target_date')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "goals": [cls._serialize_goal(g) for g in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in PerformanceGoal.Status.choices
            ],
        })

    @classmethod
    def get_goal(cls, goal_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            goal = PerformanceGoal.objects.select_related(
                'employee__user', 'created_by'
            ).get(pk=goal_id, is_deleted=False)
        except PerformanceGoal.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance goal with id {goal_id} not found"
            )

        return ServiceResponse.success(data={
            "goal": cls._serialize_goal(goal),
        })

    @classmethod
    @transaction.atomic
    def create_goal(cls,
                    employee_id: int,
                    title: str,
                    description: str = "",
                    target_date=None,
                    created_by_id: int = None) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        goal = PerformanceGoal.objects.create(
            employee_id=employee_id,
            title=title,
            description=description,
            target_date=target_date,
            status=PerformanceGoal.Status.PENDING,
            progress_percent=0,
            created_by_id=created_by_id,
        )

        goal = PerformanceGoal.objects.select_related(
            'employee__user', 'created_by'
        ).get(pk=goal.pk)

        return ServiceResponse.created(data={
            "goal": cls._serialize_goal(goal),
        }, message="Performance goal created")

    @classmethod
    @transaction.atomic
    def update_goal(cls, goal_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        try:
            goal = PerformanceGoal.objects.select_related(
                'employee__user', 'created_by'
            ).get(pk=goal_id, is_deleted=False)
        except PerformanceGoal.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance goal with id {goal_id} not found"
            )

        allowed_fields = [
            "title", "description", "target_date", "notes",
        ]

        update_fields = ["updated_at"]
        for field in allowed_fields:
            if field in kwargs:
                setattr(goal, field, kwargs[field])
                update_fields.append(field)

        goal.save(update_fields=update_fields)

        goal = PerformanceGoal.objects.select_related(
            'employee__user', 'created_by'
        ).get(pk=goal.pk)

        return ServiceResponse.success(data={
            "goal": cls._serialize_goal(goal),
        }, message="Performance goal updated")

    @classmethod
    @transaction.atomic
    def update_progress(cls,
                        goal_id: int,
                        progress_percent: int = None,
                        status: str = None) -> Tuple[Dict[str, Any], int]:
        try:
            goal = PerformanceGoal.objects.select_related(
                'employee__user', 'created_by'
            ).get(pk=goal_id, is_deleted=False)
        except PerformanceGoal.DoesNotExist:
            return ServiceResponse.not_found(
                f"Performance goal with id {goal_id} not found"
            )

        update_fields = ["updated_at"]

        if progress_percent is not None:
            if progress_percent < 0 or progress_percent > 100:
                return ServiceResponse.validation_error(
                    errors={"progress_percent": "Must be between 0 and 100"},
                )
            goal.progress_percent = progress_percent
            update_fields.append("progress_percent")

        if status is not None:
            goal.status = status
            update_fields.append("status")

        goal.save(update_fields=update_fields)

        goal = PerformanceGoal.objects.select_related(
            'employee__user', 'created_by'
        ).get(pk=goal.pk)

        return ServiceResponse.success(data={
            "goal": cls._serialize_goal(goal),
        }, message="Goal progress updated")
