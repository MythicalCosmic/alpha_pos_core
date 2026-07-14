import json
import secrets
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from base.models import Session, User
from base.repositories import SessionRepository
from hr.models import ContractDocument, Department, Employee, EmployeeContract


pytestmark = pytest.mark.django_db


def _authenticated_client(user):
    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent="contract-document-tests",
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT="contract-document-tests")
    client.cookies["session_key"] = token
    return client


@pytest.fixture
def admin_user():
    return User.objects.create(
        first_name="Admin",
        last_name="User",
        email="contract-admin@example.test",
        password="not-used",
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def admin_client(admin_user):
    return _authenticated_client(admin_user)


@pytest.fixture
def contract():
    employee_user = User.objects.create(
        first_name="Employee",
        last_name="One",
        email="contract-employee@example.test",
        password="not-used",
        role=User.RoleChoices.USER,
        status=User.UserStatus.ACTIVE,
    )
    department = Department.objects.create(name="Contract test department")
    employee = Employee.objects.create(
        user=employee_user,
        department=department,
        position="Cook",
        hire_date=date(2026, 1, 1),
    )
    return EmployeeContract.objects.create(
        employee=employee,
        contract_number="CTR-DOCUMENT-TEST-1",
        start_date=date(2026, 1, 1),
    )


@pytest.fixture(autouse=True)
def private_media(settings, tmp_path):
    media_root = tmp_path / "private-media"
    settings.MEDIA_ROOT = str(media_root)
    settings.HR_CONTRACT_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
    return media_root


def _list_url(contract):
    return f"/api/admins/hr/contracts/{contract.id}/documents/"


def _detail_url(contract, document):
    return f"{_list_url(contract)}{document.id}/"


def _pdf(name="contract.pdf", content=b"%PDF-1.4\ncontract\n%%EOF"):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def test_admin_uploads_lists_reads_and_securely_downloads_file(
    admin_client, admin_user, contract, private_media
):
    response = admin_client.post(
        _list_url(contract),
        data={
            "title": "  Signed employment contract  ",
            "document_type": "CONTRACT",
            "file": _pdf(),
        },
    )
    assert response.status_code == 201, response.content
    payload = response.json()["data"]["document"]
    assert payload["title"] == "Signed employment contract"
    assert payload["contract_id"] == contract.id
    assert payload["uploaded_by"]["id"] == admin_user.id
    assert payload["file_url"] == ""
    assert payload["download_url"].startswith(
        "/api/admins/hr/documents/file/contract_document/"
    )
    assert "hr/contracts/" not in payload["download_url"]

    document = ContractDocument.objects.get(pk=payload["id"])
    assert document.file.name.startswith("hr/contracts/")
    assert Path(document.file.path).is_file()
    assert Path(document.file.path).is_relative_to(private_media)

    list_response = admin_client.get(_list_url(contract))
    assert list_response.status_code == 200
    assert list_response.json()["data"]["count"] == 1
    assert list_response.json()["data"]["documents"][0]["id"] == document.id

    detail_response = admin_client.get(_detail_url(contract, document))
    assert detail_response.status_code == 200
    assert detail_response.json()["data"]["document"]["id"] == document.id

    download = admin_client.get(payload["download_url"])
    assert download.status_code == 200
    assert b"".join(download.streaming_content) == b"%PDF-1.4\ncontract\n%%EOF"
    assert download["Cache-Control"] == "private, no-store"
    assert download["X-Content-Type-Options"] == "nosniff"
    assert download["Content-Disposition"].startswith("attachment;")
    download.close()
    assert Client().get(payload["download_url"]).status_code == 401

    contract_payload = admin_client.get(
        f"/api/admins/hr/contracts/{contract.id}/"
    ).json()["data"]["contract"]
    assert contract_payload["documents"][0]["download_url"] == payload["download_url"]


def test_routes_require_an_active_admin_session(contract):
    assert Client().get(_list_url(contract)).status_code == 401

    manager = User.objects.create(
        first_name="Manager",
        last_name="User",
        email="contract-manager@example.test",
        password="not-used",
        role=User.RoleChoices.MANAGER,
        status=User.UserStatus.ACTIVE,
    )
    assert _authenticated_client(manager).get(_list_url(contract)).status_code == 403


def test_delete_soft_deletes_and_revokes_detail_and_download(admin_client, contract):
    created = admin_client.post(
        _list_url(contract),
        data={"title": "Termination", "document_type": "TERMINATION", "file": _pdf()},
    ).json()["data"]["document"]
    document = ContractDocument.objects.get(pk=created["id"])
    stored_path = Path(document.file.path)

    response = admin_client.delete(_detail_url(contract, document))
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.is_deleted is True
    # Soft deletion preserves the private file for sync/audit recovery, but both
    # API lookup paths immediately stop serving it.
    assert stored_path.exists()
    assert admin_client.get(_detail_url(contract, document)).status_code == 404
    assert admin_client.get(created["download_url"]).status_code == 404
    assert admin_client.get(_list_url(contract)).json()["data"]["count"] == 0


@pytest.mark.parametrize("stored_name", ["hr/contracts/missing.pdf", "../escape.pdf"])
def test_download_returns_404_for_missing_or_unsafe_storage_name(
    admin_client, contract, stored_name
):
    document = ContractDocument.objects.create(
        contract=contract,
        title="Unavailable file",
        file=stored_name,
    )
    url = f"/api/admins/hr/documents/file/contract_document/{document.id}/"
    response = admin_client.get(url)
    assert response.status_code == 404
    assert response.json()["success"] is False


def test_download_is_revoked_when_parent_contract_is_soft_deleted(
    admin_client, contract
):
    response = admin_client.post(
        _list_url(contract), data={"title": "Parent lifecycle", "file": _pdf()}
    )
    assert response.status_code == 201
    download_url = response.json()["data"]["document"]["download_url"]

    contract.delete()
    assert admin_client.get(download_url).status_code == 404


def test_nested_detail_cannot_access_a_document_from_another_contract(
    admin_client, contract
):
    second = EmployeeContract.objects.create(
        employee=contract.employee,
        contract_number="CTR-DOCUMENT-TEST-2",
        start_date=date(2026, 2, 1),
    )
    document = ContractDocument.objects.create(
        contract=second,
        title="Second contract",
        file=_pdf(),
    )
    assert admin_client.get(_detail_url(contract, document)).status_code == 404
    assert admin_client.delete(_detail_url(contract, document)).status_code == 404
    document.refresh_from_db()
    assert document.is_deleted is False


@pytest.mark.parametrize(
    ("data", "error_field"),
    [
        ({"document_type": "CONTRACT", "file": _pdf()}, "title"),
        (
            {"title": "Bad type", "document_type": "EXECUTABLE", "file": _pdf()},
            "document_type",
        ),
        ({"title": "Missing attachment", "document_type": "OTHER"}, "file"),
        ({"title": "Bad\x00title", "document_type": "OTHER", "file": _pdf()}, "title"),
    ],
)
def test_upload_validates_required_metadata(admin_client, contract, data, error_field):
    # UploadedFile instances are consumed/closed by the client, so clone any
    # parametrized PDF before making the request.
    if "file" in data:
        data = dict(data, file=_pdf())
    response = admin_client.post(_list_url(contract), data=data)
    assert response.status_code == 422
    assert error_field in response.json()["errors"]
    assert ContractDocument.objects.count() == 0


def test_upload_rejects_extension_mime_and_signature_spoofing(admin_client, contract):
    wrong_extension = SimpleUploadedFile(
        "contract.exe", b"MZ executable", content_type="application/octet-stream"
    )
    response = admin_client.post(
        _list_url(contract), data={"title": "Bad extension", "file": wrong_extension}
    )
    assert response.status_code == 422

    wrong_mime = SimpleUploadedFile(
        "contract.pdf", b"%PDF-1.4\n%%EOF", content_type="text/html"
    )
    response = admin_client.post(
        _list_url(contract), data={"title": "Bad MIME", "file": wrong_mime}
    )
    assert response.status_code == 422

    fake_pdf = SimpleUploadedFile(
        "contract.pdf", b"MZ executable", content_type="application/pdf"
    )
    response = admin_client.post(
        _list_url(contract), data={"title": "Bad signature", "file": fake_pdf}
    )
    assert response.status_code == 422
    assert ContractDocument.objects.count() == 0


def test_upload_enforces_configured_size_limit(admin_client, contract, settings):
    settings.HR_CONTRACT_DOCUMENT_MAX_BYTES = 8
    response = admin_client.post(
        _list_url(contract), data={"title": "Too large", "file": _pdf()}
    )
    assert response.status_code == 422
    assert "limit" in response.json()["errors"]["file"]
    assert ContractDocument.objects.count() == 0


def test_legacy_https_url_is_supported_but_insecure_or_credentialed_urls_are_not(
    admin_client, contract
):
    response = admin_client.post(
        _list_url(contract),
        data=json.dumps({
            "title": "Legacy archive",
            "document_type": "OTHER",
            "file_url": "https://files.example.test/contracts/old.pdf",
        }),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    document = response.json()["data"]["document"]
    assert document["download_url"] is None
    assert document["file_url"].startswith("https://")

    for bad_url in (
        "http://files.example.test/contract.pdf",
        "https://user:password@files.example.test/contract.pdf",
        "javascript:alert(1)",
    ):
        response = admin_client.post(
            _list_url(contract),
            data=json.dumps({"title": "Unsafe URL", "file_url": bad_url}),
            content_type="application/json",
        )
        assert response.status_code == 422


def test_missing_contract_returns_404_without_storing_upload(admin_client):
    response = admin_client.post(
        "/api/admins/hr/contracts/999999/documents/",
        data={"title": "Orphan", "file": _pdf()},
    )
    assert response.status_code == 404
    assert ContractDocument.objects.count() == 0
