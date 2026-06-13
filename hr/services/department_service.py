from typing import Dict, Any, Tuple
from django.db import transaction

from base.helpers.response import ServiceResponse
from hr.models import Department
from hr.repositories import DepartmentRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class DepartmentService:

    @classmethod
    def serialize(cls, department: Department) -> Dict[str, Any]:
        data = {
            "id": department.id,
            "uuid": str(department.uuid),
            "name": department.name,
            "description": department.description,
            "manager": None,
            "is_active": department.is_active,
            "employee_count": getattr(department, "employee_count", 0),
            "created_at": department.created_at.isoformat(),
            "updated_at": department.updated_at.isoformat(),
        }

        if department.manager_id and department.manager:
            data["manager"] = {
                "id": department.manager.id,
                "first_name": department.manager.first_name,
                "last_name": department.manager.last_name,
            }

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = DepartmentRepository.get_all().select_related("manager")
        queryset = DepartmentRepository.with_employee_count(queryset)

        if search:
            queryset = DepartmentRepository.search(queryset, search)

        queryset = queryset.order_by("name")

        page_obj, paginator = DepartmentRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "departments": [cls.serialize(dept) for dept in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
        })

    @classmethod
    def get(cls, department_id: int) -> Tuple[Dict[str, Any], int]:
        department = DepartmentRepository.get_by_id(department_id)
        if not department:
            return ServiceResponse.not_found(
                f"Department with id {department_id} not found"
            )

        queryset = DepartmentRepository.with_employee_count(
            Department.objects.filter(pk=department.pk, is_deleted=False)
        ).select_related("manager")
        department = queryset.first()

        return ServiceResponse.success(data={
            "department": cls.serialize(department),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               description: str = "",
               manager_id: int = None,
               is_active: bool = True) -> Tuple[Dict[str, Any], int]:
        if DepartmentRepository.name_exists(name):
            return ServiceResponse.validation_error(
                errors={"name": f"Department '{name}' already exists"},
            )

        if manager_id:
            from base.models import User
            if not User.objects.filter(pk=manager_id, is_deleted=False).exists():
                return ServiceResponse.not_found("Manager user not found")

        department = DepartmentRepository.create(
            name=name,
            description=description,
            manager_id=manager_id,
            is_active=is_active,
        )

        return ServiceResponse.created(data={
            "id": department.id,
            "uuid": str(department.uuid),
            "department": cls.serialize(department),
        }, message=f"Department '{name}' created")

    @classmethod
    @transaction.atomic
    def update(cls, department_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        department = DepartmentRepository.get_by_id(department_id)
        if not department:
            return ServiceResponse.not_found(
                f"Department with id {department_id} not found"
            )

        if "name" in kwargs and kwargs["name"] != department.name:
            if DepartmentRepository.name_exists(kwargs["name"], exclude_id=department_id):
                return ServiceResponse.validation_error(
                    errors={"name": f"Department '{kwargs['name']}' already exists"},
                )

        if "manager_id" in kwargs and kwargs["manager_id"]:
            from base.models import User
            if not User.objects.filter(pk=kwargs["manager_id"], is_deleted=False).exists():
                return ServiceResponse.not_found("Manager user not found")

        update_fields = ["updated_at"]
        for field in ["name", "description", "manager_id", "is_active"]:
            if field in kwargs:
                setattr(department, field, kwargs[field])
                update_fields.append(field)

        department.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "department": cls.serialize(department),
        }, message="Department updated")

    @classmethod
    @transaction.atomic
    def delete(cls, department_id: int) -> Tuple[Dict[str, Any], int]:
        department = DepartmentRepository.get_by_id(department_id)
        if not department:
            return ServiceResponse.not_found(
                f"Department with id {department_id} not found"
            )

        active_employees = department.employees.filter(
            is_deleted=False, is_active=True
        ).count()
        if active_employees > 0:
            return ServiceResponse.error(
                f"Cannot delete department with {active_employees} active employee(s). "
                "Reassign or deactivate them first."
            )

        department.is_active = False
        department.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": department_id,
        }, message="Department deactivated")
