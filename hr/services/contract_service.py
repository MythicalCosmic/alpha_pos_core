from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import EmployeeContract, EmploymentEvent
from hr.repositories import EmployeeRepository
from hr.repositories.contract import ContractRepository
from hr.repositories.contract_document import ContractDocumentRepository


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class ContractService:

    @classmethod
    def _serialize(cls, contract: EmployeeContract, include_documents: bool = False) -> Dict[str, Any]:
        data = {
            "id": contract.id,
            "uuid": str(contract.uuid),
            "employee": None,
            "contract_number": contract.contract_number,
            "start_date": contract.start_date.isoformat(),
            "end_date": contract.end_date.isoformat() if contract.end_date else None,
            "probation_end_date": contract.probation_end_date.isoformat() if contract.probation_end_date else None,
            "contract_type": contract.contract_type,
            "contract_type_display": contract.get_contract_type_display(),
            "status": contract.status,
            "status_display": contract.get_status_display(),
            "salary_amount": str(contract.salary_amount),
            "position_title": contract.position_title,
            "terms": contract.terms,
            "termination_date": contract.termination_date.isoformat() if contract.termination_date else None,
            "termination_reason": contract.termination_reason,
            "renewed_from_id": contract.renewed_from_id,
            "created_by": None,
            "notes": contract.notes,
            "created_at": contract.created_at.isoformat(),
            "updated_at": contract.updated_at.isoformat(),
        }

        if contract.employee_id and contract.employee:
            emp = contract.employee
            emp_data = {
                "id": emp.id,
                "position": emp.position,
            }
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        if contract.created_by_id and contract.created_by:
            data["created_by"] = {
                "id": contract.created_by.id,
                "first_name": contract.created_by.first_name,
                "last_name": contract.created_by.last_name,
            }

        if include_documents:
            docs = ContractDocumentRepository.get_for_contract(contract.id)
            data["documents"] = [
                {
                    "id": doc.id,
                    "uuid": str(doc.uuid),
                    "title": doc.title,
                    "document_type": doc.document_type,
                    "document_type_display": doc.get_document_type_display(),
                    "file_url": doc.file_url,
                    "uploaded_by": {
                        "id": doc.uploaded_by.id,
                        "first_name": doc.uploaded_by.first_name,
                        "last_name": doc.uploaded_by.last_name,
                    } if doc.uploaded_by_id and doc.uploaded_by else None,
                    "uploaded_at": doc.uploaded_at.isoformat(),
                }
                for doc in docs
            ]
        else:
            data["documents"] = []

        return data

    @classmethod
    def _generate_number(cls) -> str:
        today = timezone.now()
        date_part = today.strftime("%Y%m%d")
        prefix = "CTR"
        filter_kwargs = {"contract_number__startswith": f"{prefix}-{date_part}"}
        last = EmployeeContract.objects.filter(**filter_kwargs).order_by("-contract_number").first()

        if last:
            try:
                seq = int(last.contract_number.split("-")[-1]) + 1
            except Exception:
                seq = 1
        else:
            seq = 1

        return f"{prefix}-{date_part}-{seq:04d}"

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             employee_id: int = None,
             status: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = EmployeeContract.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'created_by')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if status:
            queryset = queryset.filter(status=status)

        queryset = queryset.order_by('-start_date')

        page_obj, paginator = ContractRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "contracts": [cls._serialize(c) for c in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [
                {"value": c[0], "label": c[1]}
                for c in EmployeeContract.Status.choices
            ],
        })

    @classmethod
    def get(cls, contract_id: int) -> Tuple[Dict[str, Any], int]:
        contract = ContractRepository.get_with_relations(contract_id)
        if not contract:
            return ServiceResponse.not_found(
                f"Contract with id {contract_id} not found"
            )

        return ServiceResponse.success(data={
            "contract": cls._serialize(contract, include_documents=True),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               employee_id: int,
               start_date,
               end_date=None,
               probation_end_date=None,
               contract_type: str = "INITIAL",
               salary_amount: Decimal = Decimal("0"),
               position_title: str = "",
               terms: str = "",
               created_by_id: int = None) -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        contract_number = cls._generate_number()

        contract = ContractRepository.create(
            employee_id=employee_id,
            contract_number=contract_number,
            start_date=start_date,
            end_date=end_date,
            probation_end_date=probation_end_date,
            contract_type=contract_type,
            status=EmployeeContract.Status.DRAFT,
            salary_amount=Decimal(str(salary_amount)),
            position_title=position_title,
            terms=terms,
            created_by_id=created_by_id,
        )

        contract = ContractRepository.get_with_relations(contract.pk)

        return ServiceResponse.created(data={
            "id": contract.id,
            "uuid": str(contract.uuid),
            "contract": cls._serialize(contract),
        }, message="Contract created")

    @classmethod
    @transaction.atomic
    def update(cls, contract_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        contract = ContractRepository.get_with_relations(contract_id)
        if not contract:
            return ServiceResponse.not_found(
                f"Contract with id {contract_id} not found"
            )

        if contract.status != EmployeeContract.Status.DRAFT:
            return ServiceResponse.error(
                "Can only update contracts in DRAFT status"
            )

        allowed_fields = [
            "start_date", "end_date", "probation_end_date", "contract_type",
            "salary_amount", "position_title", "terms", "notes",
        ]

        update_fields = ["updated_at"]
        for field in allowed_fields:
            if field in kwargs:
                value = kwargs[field]
                if field == "salary_amount":
                    value = Decimal(str(value))
                setattr(contract, field, value)
                update_fields.append(field)

        contract.save(update_fields=update_fields)
        contract = ContractRepository.get_with_relations(contract.pk)

        return ServiceResponse.success(data={
            "contract": cls._serialize(contract),
        }, message="Contract updated")

    @classmethod
    @transaction.atomic
    def activate(cls, contract_id: int) -> Tuple[Dict[str, Any], int]:
        contract = ContractRepository.get_with_relations(contract_id)
        if not contract:
            return ServiceResponse.not_found(
                f"Contract with id {contract_id} not found"
            )

        if contract.status != EmployeeContract.Status.DRAFT:
            return ServiceResponse.error(
                "Can only activate contracts in DRAFT status"
            )

        contract.status = EmployeeContract.Status.ACTIVE
        contract.save(update_fields=["status", "updated_at"])

        event_type = EmploymentEvent.EventType.CONTRACT_RENEWED \
            if contract.renewed_from_id else EmploymentEvent.EventType.HIRED

        EmploymentEvent.objects.create(
            employee_id=contract.employee_id,
            event_type=event_type,
            event_date=timezone.now().date(),
            description=f"Contract {contract.contract_number} activated",
            new_value=contract.contract_number,
            created_by_id=contract.created_by_id,
        )

        contract = ContractRepository.get_with_relations(contract.pk)

        return ServiceResponse.success(data={
            "contract": cls._serialize(contract),
        }, message="Contract activated")

    @classmethod
    @transaction.atomic
    def terminate(cls,
                  contract_id: int,
                  termination_date=None,
                  termination_reason: str = "",
                  user_id: int = None) -> Tuple[Dict[str, Any], int]:
        contract = ContractRepository.get_with_relations(contract_id)
        if not contract:
            return ServiceResponse.not_found(
                f"Contract with id {contract_id} not found"
            )

        if contract.status != EmployeeContract.Status.ACTIVE:
            return ServiceResponse.error(
                "Can only terminate contracts in ACTIVE status"
            )

        if termination_date is None:
            termination_date = timezone.now().date()

        contract.status = EmployeeContract.Status.TERMINATED
        contract.termination_date = termination_date
        contract.termination_reason = termination_reason
        contract.save(update_fields=[
            "status", "termination_date", "termination_reason", "updated_at",
        ])

        EmploymentEvent.objects.create(
            employee_id=contract.employee_id,
            event_type=EmploymentEvent.EventType.CONTRACT_TERMINATED,
            event_date=termination_date,
            description=f"Contract {contract.contract_number} terminated: {termination_reason}",
            old_value=contract.contract_number,
            created_by_id=user_id,
        )

        contract = ContractRepository.get_with_relations(contract.pk)

        return ServiceResponse.success(data={
            "contract": cls._serialize(contract),
        }, message="Contract terminated")

    @classmethod
    @transaction.atomic
    def renew(cls,
              contract_id: int,
              new_start_date=None,
              new_end_date=None,
              new_salary: Decimal = None,
              user_id: int = None) -> Tuple[Dict[str, Any], int]:
        old_contract = ContractRepository.get_with_relations(contract_id)
        if not old_contract:
            return ServiceResponse.not_found(
                f"Contract with id {contract_id} not found"
            )

        if old_contract.status != EmployeeContract.Status.ACTIVE:
            return ServiceResponse.error(
                "Can only renew contracts in ACTIVE status"
            )

        old_contract.status = EmployeeContract.Status.RENEWED
        old_contract.save(update_fields=["status", "updated_at"])

        salary = Decimal(str(new_salary)) if new_salary is not None else old_contract.salary_amount
        contract_number = cls._generate_number()

        new_contract = ContractRepository.create(
            employee_id=old_contract.employee_id,
            contract_number=contract_number,
            start_date=new_start_date or timezone.now().date(),
            end_date=new_end_date,
            contract_type=EmployeeContract.ContractType.RENEWAL,
            status=EmployeeContract.Status.ACTIVE,
            salary_amount=salary,
            position_title=old_contract.position_title,
            terms=old_contract.terms,
            renewed_from_id=old_contract.id,
            created_by_id=user_id,
        )

        EmploymentEvent.objects.create(
            employee_id=old_contract.employee_id,
            event_type=EmploymentEvent.EventType.CONTRACT_RENEWED,
            event_date=timezone.now().date(),
            description=f"Contract renewed: {old_contract.contract_number} -> {contract_number}",
            old_value=old_contract.contract_number,
            new_value=contract_number,
            created_by_id=user_id,
        )

        new_contract = ContractRepository.get_with_relations(new_contract.pk)

        return ServiceResponse.created(data={
            "old_contract_id": old_contract.id,
            "contract": cls._serialize(new_contract),
        }, message="Contract renewed")

    @classmethod
    def get_expiring(cls, days: int = 30) -> Tuple[Dict[str, Any], int]:
        contracts = ContractRepository.get_expiring(days).select_related(
            'employee__user', 'created_by'
        )

        return ServiceResponse.success(data={
            "contracts": [cls._serialize(c) for c in contracts],
            "count": contracts.count(),
            "days": days,
        })

    @classmethod
    def get_expired(cls) -> Tuple[Dict[str, Any], int]:
        contracts = ContractRepository.get_expired().select_related(
            'employee__user', 'created_by'
        )

        return ServiceResponse.success(data={
            "contracts": [cls._serialize(c) for c in contracts],
            "count": contracts.count(),
        })
