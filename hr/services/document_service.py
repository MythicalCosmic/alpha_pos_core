from typing import Dict, Any, Tuple
from datetime import timedelta
from django.db import transaction
from django.core.paginator import Paginator
from django.utils import timezone

from base.helpers.response import ServiceResponse
from hr.models import EmployeeDocument, Employee
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


class DocumentService:

    @classmethod
    def _serialize(cls, doc: EmployeeDocument) -> Dict[str, Any]:
        data = {
            "id": doc.id,
            "uuid": str(doc.uuid),
            "employee": None,
            "document_type": doc.document_type,
            "document_type_display": doc.get_document_type_display(),
            "title": doc.title,
            "file_url": doc.file_url,
            "issue_date": doc.issue_date.isoformat() if doc.issue_date else None,
            "expiry_date": doc.expiry_date.isoformat() if doc.expiry_date else None,
            "is_verified": doc.is_verified,
            "verified_by": None,
            "notes": doc.notes,
            "uploaded_at": doc.uploaded_at.isoformat(),
        }

        if doc.employee_id and doc.employee:
            emp = doc.employee
            emp_data = {"id": emp.id, "position": emp.position}
            if emp.user:
                emp_data["user"] = {
                    "id": emp.user.id,
                    "first_name": emp.user.first_name,
                    "last_name": emp.user.last_name,
                }
            data["employee"] = emp_data

        if doc.is_verified and doc.verified_by_id and doc.verified_by:
            data["verified_by"] = {
                "id": doc.verified_by.id,
                "first_name": doc.verified_by.first_name,
                "last_name": doc.verified_by.last_name,
            }

        return data

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             employee_id: int = None,
             document_type: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = EmployeeDocument.objects.filter(
            is_deleted=False
        ).select_related('employee__user', 'verified_by')

        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        if document_type:
            queryset = queryset.filter(document_type=document_type)

        queryset = queryset.order_by('-uploaded_at')

        paginator = Paginator(queryset, per_page)
        page_obj = paginator.get_page(page)

        return ServiceResponse.success(data={
            "documents": [cls._serialize(d) for d in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "document_types": [
                {"value": c[0], "label": c[1]}
                for c in EmployeeDocument.DocumentType.choices
            ],
        })

    @classmethod
    def get(cls, doc_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            doc = EmployeeDocument.objects.select_related(
                'employee__user', 'verified_by'
            ).get(pk=doc_id, is_deleted=False)
        except EmployeeDocument.DoesNotExist:
            return ServiceResponse.not_found(
                f"Document with id {doc_id} not found"
            )

        return ServiceResponse.success(data={
            "document": cls._serialize(doc),
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               employee_id: int,
               document_type: str = "OTHER",
               title: str = "",
               file_url: str = "",
               issue_date=None,
               expiry_date=None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:
        employee = EmployeeRepository.get_by_id(employee_id)
        if not employee:
            return ServiceResponse.not_found("Employee not found")

        doc = EmployeeDocument.objects.create(
            employee_id=employee_id,
            document_type=document_type,
            title=title,
            file_url=file_url,
            issue_date=issue_date,
            expiry_date=expiry_date,
            notes=notes,
        )

        doc = EmployeeDocument.objects.select_related(
            'employee__user', 'verified_by'
        ).get(pk=doc.pk)

        return ServiceResponse.created(data={
            "document": cls._serialize(doc),
        }, message="Document created")

    @classmethod
    @transaction.atomic
    def update(cls, doc_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        try:
            doc = EmployeeDocument.objects.select_related(
                'employee__user', 'verified_by'
            ).get(pk=doc_id, is_deleted=False)
        except EmployeeDocument.DoesNotExist:
            return ServiceResponse.not_found(
                f"Document with id {doc_id} not found"
            )

        allowed_fields = [
            "document_type", "title", "file_url",
            "issue_date", "expiry_date", "notes",
        ]

        changed = False
        for field in allowed_fields:
            if field in kwargs:
                setattr(doc, field, kwargs[field])
                changed = True

        if changed:
            doc.save()

        doc = EmployeeDocument.objects.select_related(
            'employee__user', 'verified_by'
        ).get(pk=doc.pk)

        return ServiceResponse.success(data={
            "document": cls._serialize(doc),
        }, message="Document updated")

    @classmethod
    @transaction.atomic
    def delete(cls, doc_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            doc = EmployeeDocument.objects.get(pk=doc_id, is_deleted=False)
        except EmployeeDocument.DoesNotExist:
            return ServiceResponse.not_found(
                f"Document with id {doc_id} not found"
            )

        doc.is_deleted = True
        doc.save(update_fields=["is_deleted"])

        return ServiceResponse.success(data={
            "id": doc_id,
        }, message="Document deleted")

    @classmethod
    @transaction.atomic
    def verify(cls,
               doc_id: int,
               verified_by_id: int) -> Tuple[Dict[str, Any], int]:
        try:
            doc = EmployeeDocument.objects.select_related(
                'employee__user', 'verified_by'
            ).get(pk=doc_id, is_deleted=False)
        except EmployeeDocument.DoesNotExist:
            return ServiceResponse.not_found(
                f"Document with id {doc_id} not found"
            )

        doc.is_verified = True
        doc.verified_by_id = verified_by_id
        doc.save(update_fields=["is_verified", "verified_by_id"])

        doc = EmployeeDocument.objects.select_related(
            'employee__user', 'verified_by'
        ).get(pk=doc.pk)

        return ServiceResponse.success(data={
            "document": cls._serialize(doc),
        }, message="Document verified")

    @classmethod
    def get_expiring(cls, days: int = 30) -> Tuple[Dict[str, Any], int]:
        today = timezone.now().date()
        end = today + timedelta(days=days)

        docs = EmployeeDocument.objects.filter(
            expiry_date__range=(today, end),
            is_deleted=False,
        ).select_related('employee__user', 'verified_by').order_by('expiry_date')

        return ServiceResponse.success(data={
            "documents": [cls._serialize(d) for d in docs],
            "count": docs.count(),
            "days": days,
        })

    @classmethod
    def get_by_employee(cls, employee_id: int) -> Tuple[Dict[str, Any], int]:
        if not Employee.objects.filter(pk=employee_id, is_deleted=False).exists():
            return ServiceResponse.not_found("Employee not found")

        docs = EmployeeDocument.objects.filter(
            employee_id=employee_id, is_deleted=False
        ).select_related('employee__user', 'verified_by').order_by('-uploaded_at')

        return ServiceResponse.success(data={
            "documents": [cls._serialize(d) for d in docs],
            "count": docs.count(),
            "employee_id": employee_id,
        })
