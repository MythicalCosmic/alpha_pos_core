from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction

from base.helpers.response import ServiceResponse
from hr.models import Employee
from hr.repositories import EmployeeRepository, DepartmentRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class EmployeeService:

    @classmethod
    def serialize(cls, employee: Employee) -> Dict[str, Any]:
        data = {
            "id": employee.id,
            "uuid": str(employee.uuid),
            "user": None,
            "department": None,
            "position": employee.position,
            "hire_date": employee.hire_date.isoformat(),
            "contract_type": employee.contract_type,
            "base_salary": str(employee.base_salary),
            "payment_frequency": employee.payment_frequency,
            "phone": employee.phone,
            "address": employee.address,
            "emergency_contact_name": employee.emergency_contact_name,
            "emergency_contact_phone": employee.emergency_contact_phone,
            "bank_account": employee.bank_account,
            "bank_name": employee.bank_name,
            "is_active": employee.is_active,
            "notes": employee.notes,
            "created_at": employee.created_at.isoformat(),
            "updated_at": employee.updated_at.isoformat(),
        }

        if employee.user_id and employee.user:
            data["user"] = {
                "id": employee.user.id,
                "first_name": employee.user.first_name,
                "last_name": employee.user.last_name,
                "email": employee.user.email,
                "role": employee.user.role,
            }

        if employee.department_id and employee.department:
            data["department"] = {
                "id": employee.department.id,
                "name": employee.department.name,
            }

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             department_id: int = None,
             contract_type: str = None,
             is_active: bool = None) -> Tuple[Dict[str, Any], int]:
        queryset = Employee.objects.filter(
            is_deleted=False
        ).select_related("user", "department")

        if search:
            queryset = EmployeeRepository.search(queryset, search)

        if department_id:
            queryset = queryset.filter(department_id=department_id)

        if contract_type:
            queryset = queryset.filter(contract_type=contract_type)

        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)

        queryset = queryset.order_by("user__first_name", "user__last_name")

        page_obj, paginator = EmployeeRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "employees": [cls.serialize(emp) for emp in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "contract_types": [
                {"value": c[0], "label": c[1]}
                for c in Employee.ContractType.choices
            ],
        })

    @classmethod
    def get(cls, employee_id: int) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_with_relations(employee_id)
        if not employee:
            return ServiceResponse.not_found(
                f"Employee with id {employee_id} not found"
            )

        return ServiceResponse.success(data={
            "employee": cls.serialize(employee),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               user_id: int,
               position: str,
               hire_date,
               department_id: int = None,
               contract_type: str = "FULL_TIME",
               base_salary: Decimal = Decimal("0"),
               payment_frequency: str = "MONTHLY",
               phone: str = "",
               address: str = "",
               emergency_contact_name: str = "",
               emergency_contact_phone: str = "",
               bank_account: str = "",
               bank_name: str = "",
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        from base.models import User
        if not User.objects.filter(pk=user_id, is_deleted=False).exists():
            return ServiceResponse.not_found("User not found")

        if EmployeeRepository.has_employee_profile(user_id):
            return ServiceResponse.validation_error(
                errors={"user_id": "This user already has an employee profile"},
            )

        if department_id:
            department = DepartmentRepository.get_by_id(department_id)
            if not department:
                return ServiceResponse.not_found("Department not found")

        employee = EmployeeRepository.create(
            user_id=user_id,
            department_id=department_id,
            position=position,
            hire_date=hire_date,
            contract_type=contract_type,
            base_salary=Decimal(str(base_salary)),
            payment_frequency=payment_frequency,
            phone=phone,
            address=address,
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
            bank_account=bank_account,
            bank_name=bank_name,
            notes=notes,
        )

        employee = EmployeeRepository.get_with_relations(employee.pk)

        return ServiceResponse.created(data={
            "id": employee.id,
            "uuid": str(employee.uuid),
            "employee": cls.serialize(employee),
        }, message="Employee profile created")

    @classmethod
    @transaction.atomic
    def update(cls, employee_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_with_relations(employee_id)
        if not employee:
            return ServiceResponse.not_found(
                f"Employee with id {employee_id} not found"
            )

        allowed_fields = [
            "department_id", "position", "hire_date", "contract_type",
            "base_salary", "payment_frequency", "phone", "address",
            "emergency_contact_name", "emergency_contact_phone",
            "bank_account", "bank_name", "is_active", "notes",
        ]

        if "department_id" in kwargs and kwargs["department_id"]:
            department = DepartmentRepository.get_by_id(kwargs["department_id"])
            if not department:
                return ServiceResponse.not_found("Department not found")

        update_fields = ["updated_at"]
        for field in allowed_fields:
            if field in kwargs:
                value = kwargs[field]
                if field == "base_salary":
                    value = Decimal(str(value))
                setattr(employee, field, value)
                update_fields.append(field)

        employee.save(update_fields=update_fields)

        employee = EmployeeRepository.get_with_relations(employee.pk)

        return ServiceResponse.success(data={
            "employee": cls.serialize(employee),
        }, message="Employee updated")

    @classmethod
    @transaction.atomic
    def delete(cls, employee_id: int) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found(
                f"Employee with id {employee_id} not found"
            )

        # Set both flags so the row drops out of every default list/get
        # path (is_deleted=False filters) AND out of "active employees"
        # queries. Previously only is_active was flipped, so the endpoint
        # named "delete" left the row visible in detail/list responses
        # and still mutable via update.
        employee.is_active = False
        employee.is_deleted = True
        employee.save(update_fields=["is_active", "is_deleted", "updated_at"])

        return ServiceResponse.success(data={
            "id": employee_id,
        }, message="Employee deleted")

    @classmethod
    def get_stats(cls) -> Tuple[Dict[str, Any], int]:
        stats = EmployeeRepository.get_stats()

        return ServiceResponse.success(data={
            "stats": stats,
        })

    @classmethod
    @transaction.atomic
    def update_salary(cls,
                      employee_id: int,
                      base_salary: Decimal) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found(
                f"Employee with id {employee_id} not found"
            )

        base_salary = Decimal(str(base_salary))
        if base_salary < 0:
            return ServiceResponse.validation_error(
                errors={"base_salary": "Salary cannot be negative"},
            )

        old_salary = employee.base_salary
        employee.base_salary = base_salary
        employee.save(update_fields=["base_salary", "updated_at"])

        return ServiceResponse.success(data={
            "employee_id": employee_id,
            "old_salary": str(old_salary),
            "new_salary": str(base_salary),
        }, message="Salary updated")
