from typing import Dict, Any, Tuple
from django.db import transaction
from django.core.paginator import Paginator

from base.helpers.response import ServiceResponse
from hr.models import EmploymentEvent, Employee


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class EmploymentEventService:

    @classmethod
    def _serialize(cls, event: EmploymentEvent) -> Dict[str, Any]:
        data = {
            "id": event.id,
            "uuid": str(event.uuid),
            "employee": None,
            "event_type": event.event_type,
            "event_type_display": event.get_event_type_display(),
            "event_date": event.event_date.isoformat(),
            "description": event.description,
            "old_value": event.old_value,
            "new_value": event.new_value,
            "created_by": None,
            "created_at": event.created_at.isoformat(),
        }

        if event.employee_id and event.employee:
            emp = event.employee
            name = ""
            if emp.user:
                name = f"{emp.user.first_name} {emp.user.last_name}"
            data["employee"] = {
                "id": emp.id,
                "name": name,
            }

        if event.created_by_id and event.created_by:
            data["created_by"] = {
                "id": event.created_by.id,
                "first_name": event.created_by.first_name,
                "last_name": event.created_by.last_name,
            }

        return data

    @classmethod
    @transaction.atomic
    def log_event(cls,
                  employee_id: int,
                  event_type: str,
                  event_date,
                  description: str = "",
                  old_value: str = "",
                  new_value: str = "",
                  created_by_id: int = None) -> Tuple[Dict[str, Any], int]:
        if not Employee.objects.filter(pk=employee_id, is_deleted=False).exists():
            return ServiceResponse.not_found("Employee not found")

        event = EmploymentEvent.objects.create(
            employee_id=employee_id,
            event_type=event_type,
            event_date=event_date,
            description=description,
            old_value=old_value,
            new_value=new_value,
            created_by_id=created_by_id,
        )

        event = EmploymentEvent.objects.select_related(
            'employee__user', 'created_by'
        ).get(pk=event.pk)

        return ServiceResponse.created(data={
            "event": cls._serialize(event),
        }, message="Employment event logged")

    @classmethod
    def get_timeline(cls,
                     employee_id: int,
                     page: int = 1,
                     per_page: int = 20) -> Tuple[Dict[str, Any], int]:
        if not Employee.objects.filter(pk=employee_id, is_deleted=False).exists():
            return ServiceResponse.not_found("Employee not found")

        queryset = EmploymentEvent.objects.filter(
            employee_id=employee_id, is_deleted=False
        ).select_related('employee__user', 'created_by').order_by('-event_date', '-created_at')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "events": [cls._serialize(e) for e in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
        })

    @classmethod
    def get_event(cls, event_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            event = EmploymentEvent.objects.select_related(
                'employee__user', 'created_by'
            ).get(pk=event_id, is_deleted=False)
        except EmploymentEvent.DoesNotExist:
            return ServiceResponse.not_found("Employment event not found")
        return ServiceResponse.success(data={"event": cls._serialize(event)})

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             employee_id: int = None,
             event_type: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = EmploymentEvent.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'created_by')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if event_type:
            queryset = queryset.filter(event_type=event_type)

        queryset = queryset.order_by('-event_date', '-created_at')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "events": [cls._serialize(e) for e in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "event_types": [
                {"value": c[0], "label": c[1]}
                for c in EmploymentEvent.EventType.choices
            ],
        })
