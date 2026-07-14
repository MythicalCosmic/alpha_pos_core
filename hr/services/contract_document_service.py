"""Validated, private attachments for employee contracts."""

import logging
from pathlib import PurePath
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.urls import reverse
from django.utils.text import get_valid_filename

from base.helpers.response import ServiceResponse
from hr.models import ContractDocument, EmployeeContract
from hr.repositories.contract_document import ContractDocumentRepository


DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)

# Contract records commonly arrive as a PDF, a scan, or an editable Word
# document. Extension, declared MIME type, and file signature must all agree;
# accepting only the browser-provided MIME type would allow renamed executables.
_UPLOAD_TYPES = {
    ".pdf": {
        "mime_types": {"application/pdf"},
        "signature": lambda header: header.startswith(b"%PDF-"),
    },
    ".png": {
        "mime_types": {"image/png"},
        "signature": lambda header: header.startswith(b"\x89PNG\r\n\x1a\n"),
    },
    ".jpg": {
        "mime_types": {"image/jpeg", "image/pjpeg"},
        "signature": lambda header: header.startswith(b"\xff\xd8\xff"),
    },
    ".jpeg": {
        "mime_types": {"image/jpeg", "image/pjpeg"},
        "signature": lambda header: header.startswith(b"\xff\xd8\xff"),
    },
    ".doc": {
        "mime_types": {"application/msword", "application/vnd.ms-office"},
        "signature": lambda header: header.startswith(
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        ),
    },
    ".docx": {
        "mime_types": {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
        "signature": lambda header: header.startswith(b"PK\x03\x04"),
    },
}


class ContractDocumentService:
    @staticmethod
    def _max_upload_bytes() -> int:
        configured = getattr(
            settings, "HR_CONTRACT_DOCUMENT_MAX_BYTES", DEFAULT_MAX_UPLOAD_BYTES
        )
        try:
            configured = int(configured)
        except (TypeError, ValueError):
            return DEFAULT_MAX_UPLOAD_BYTES
        return configured if configured > 0 else DEFAULT_MAX_UPLOAD_BYTES

    @classmethod
    def serialize(cls, document: ContractDocument) -> Dict[str, Any]:
        has_file = bool(document.file and document.file.name)
        return {
            "id": document.id,
            "uuid": str(document.uuid),
            "contract_id": document.contract_id,
            "title": document.title,
            "document_type": document.document_type,
            "document_type_display": document.get_document_type_display(),
            # Retained only for records created by the legacy API.
            "file_url": document.file_url or "",
            "download_url": reverse(
                "hr:document-download",
                kwargs={"kind": "contract_document", "obj_id": document.id},
            ) if has_file else None,
            "file_name": PurePath(document.file.name).name if has_file else None,
            "uploaded_by": {
                "id": document.uploaded_by.id,
                "first_name": document.uploaded_by.first_name,
                "last_name": document.uploaded_by.last_name,
            } if document.uploaded_by_id and document.uploaded_by else None,
            "uploaded_at": document.uploaded_at.isoformat(),
        }

    @staticmethod
    def _get_contract(contract_id: int):
        return EmployeeContract.objects.filter(
            pk=contract_id, is_deleted=False
        ).first()

    @staticmethod
    def _get_document(contract_id: int, document_id: int):
        return ContractDocument.objects.select_related("uploaded_by").filter(
            pk=document_id,
            contract_id=contract_id,
            is_deleted=False,
        ).first()

    @classmethod
    def list(cls, contract_id: int) -> Tuple[Dict[str, Any], int]:
        if not cls._get_contract(contract_id):
            return ServiceResponse.not_found("Contract not found")

        documents = ContractDocumentRepository.get_for_contract(contract_id)
        return ServiceResponse.success(data={
            "documents": [cls.serialize(document) for document in documents],
            "count": documents.count(),
            "document_types": [
                {"value": value, "label": label}
                for value, label in ContractDocument.DocumentType.choices
            ],
        })

    @classmethod
    def get(cls, contract_id: int, document_id: int) -> Tuple[Dict[str, Any], int]:
        if not cls._get_contract(contract_id):
            return ServiceResponse.not_found("Contract not found")

        document = cls._get_document(contract_id, document_id)
        if not document:
            return ServiceResponse.not_found("Contract document not found")
        return ServiceResponse.success(data={"document": cls.serialize(document)})

    @classmethod
    def _validate_title(cls, title: Any, errors: Dict[str, str]) -> str:
        if not isinstance(title, str):
            errors["title"] = "Title must be a string."
            return ""
        title = title.strip()
        if not title:
            errors["title"] = "Title is required."
        elif any(ord(character) < 32 for character in title):
            errors["title"] = "Title must not contain control characters."
        elif len(title) > 200:
            errors["title"] = "Title must not exceed 200 characters."
        return title

    @classmethod
    def _validate_document_type(
        cls, document_type: Any, errors: Dict[str, str]
    ) -> str:
        valid_types = set(ContractDocument.DocumentType.values)
        if not isinstance(document_type, str) or document_type not in valid_types:
            errors["document_type"] = (
                "Document type must be one of: " + ", ".join(sorted(valid_types)) + "."
            )
            return ""
        return document_type

    @classmethod
    def _validate_legacy_url(cls, file_url: Any, errors: Dict[str, str]) -> str:
        if file_url in (None, ""):
            return ""
        if not isinstance(file_url, str):
            errors["file_url"] = "File URL must be a string."
            return ""
        file_url = file_url.strip()
        if len(file_url) > 500:
            errors["file_url"] = "File URL must not exceed 500 characters."
            return ""
        try:
            URLValidator(schemes=("https",))(file_url)
        except ValidationError:
            errors["file_url"] = "Legacy file URL must be a valid HTTPS URL."
            return ""
        parsed = urlsplit(file_url)
        if parsed.username or parsed.password:
            errors["file_url"] = "File URL must not contain credentials."
            return ""
        return file_url

    @classmethod
    def _validate_upload(cls, uploaded_file, errors: Dict[str, str]):
        if uploaded_file is None:
            return None

        size = getattr(uploaded_file, "size", None)
        if not isinstance(size, int) or size <= 0:
            errors["file"] = "Uploaded file must not be empty."
            return None
        max_bytes = cls._max_upload_bytes()
        if size > max_bytes:
            errors["file"] = f"Uploaded file exceeds the {max_bytes}-byte limit."
            return None

        raw_name = str(getattr(uploaded_file, "name", "") or "")
        # Strip both POSIX and Windows path components before handing the name to
        # storage, then let Django remove unsafe punctuation.
        base_name = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
        if not base_name or "\x00" in base_name or len(base_name) > 255:
            errors["file"] = "Uploaded file name is invalid."
            return None
        extension = PurePath(base_name).suffix.lower()
        type_config = _UPLOAD_TYPES.get(extension)
        if type_config is None:
            errors["file"] = "Allowed file types are PDF, PNG, JPEG, DOC, and DOCX."
            return None

        content_type = str(getattr(uploaded_file, "content_type", "") or "")
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type not in type_config["mime_types"]:
            errors["file"] = "File extension and content type do not match."
            return None

        try:
            original_position = uploaded_file.tell()
        except (AttributeError, OSError):
            original_position = 0
        try:
            uploaded_file.seek(0)
            header = uploaded_file.read(32)
            if not type_config["signature"](header):
                errors["file"] = "File contents do not match the declared file type."
                return None
            if extension == ".docx":
                uploaded_file.seek(0)
                try:
                    with ZipFile(uploaded_file) as archive:
                        members = set(archive.namelist())
                    if not {"[Content_Types].xml", "word/document.xml"}.issubset(members):
                        errors["file"] = "DOCX file is not a valid Word document."
                        return None
                except (BadZipFile, OSError, ValueError):
                    errors["file"] = "DOCX file is not a valid Word document."
                    return None
        except (AttributeError, OSError):
            errors["file"] = "Uploaded file could not be read."
            return None
        finally:
            try:
                uploaded_file.seek(original_position)
            except (AttributeError, OSError):
                pass

        safe_name = get_valid_filename(base_name)
        if not safe_name or PurePath(safe_name).suffix.lower() != extension:
            errors["file"] = "Uploaded file name is invalid."
            return None
        uploaded_file.name = safe_name
        return uploaded_file

    @classmethod
    @transaction.atomic
    def create(
        cls,
        contract_id: int,
        *,
        title: Any,
        document_type: Any = ContractDocument.DocumentType.CONTRACT,
        uploaded_file=None,
        file_url: Any = "",
        uploaded_by_id: int = None,
    ) -> Tuple[Dict[str, Any], int]:
        if not cls._get_contract(contract_id):
            return ServiceResponse.not_found("Contract not found")

        errors: Dict[str, str] = {}
        title = cls._validate_title(title, errors)
        document_type = cls._validate_document_type(document_type, errors)
        file_url = cls._validate_legacy_url(file_url, errors)
        uploaded_file = cls._validate_upload(uploaded_file, errors)
        if uploaded_file and file_url:
            errors["file"] = "Provide either an uploaded file or a legacy URL, not both."
        elif uploaded_file is None and not file_url and "file" not in errors:
            errors["file"] = "A file upload or legacy HTTPS URL is required."
        if errors:
            return ServiceResponse.validation_error(errors)

        document = ContractDocument(
            contract_id=contract_id,
            title=title,
            document_type=document_type,
            file=uploaded_file,
            file_url=file_url,
            uploaded_by_id=uploaded_by_id,
        )
        try:
            document.save()
        except Exception:
            # Django's DB transaction cannot roll back storage. If sync enqueue
            # or the INSERT fails after FileField has written the blob, remove
            # that newly allocated blob so failed uploads do not leak forever.
            if (
                document.file
                and document.file.name
                and getattr(document.file, "_committed", False)
            ):
                try:
                    document.file.delete(save=False)
                except Exception:
                    logger.warning(
                        "Could not remove orphaned contract upload %s",
                        document.file.name,
                        exc_info=True,
                    )
            raise
        document = cls._get_document(contract_id, document.id)
        return ServiceResponse.created(
            data={"document": cls.serialize(document)},
            message="Contract document uploaded",
        )

    @classmethod
    @transaction.atomic
    def delete(cls, contract_id: int, document_id: int) -> Tuple[Dict[str, Any], int]:
        if not cls._get_contract(contract_id):
            return ServiceResponse.not_found("Contract not found")

        document = cls._get_document(contract_id, document_id)
        if not document:
            return ServiceResponse.not_found("Contract document not found")
        document.delete()
        return ServiceResponse.success(
            data={"id": document_id}, message="Contract document deleted"
        )
