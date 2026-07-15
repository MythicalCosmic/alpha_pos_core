import json
import secrets
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from base.models import Session
from base.repositories import SessionRepository
from stock.models import StockLocation, StockUnit, VarianceReasonCode


pytestmark = pytest.mark.django_db


def _authenticated_client(user):
    token = secrets.token_hex(32)
    user_agent = "stock-catalog-contract-tests"
    Session.objects.create(
        user_id=user,
        ip_address="127.0.0.1",
        user_agent=user_agent,
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT=user_agent)
    client.cookies["session_key"] = token
    return client


@pytest.fixture
def admin_client(admin_user):
    return _authenticated_client(admin_user)


def test_variance_code_detail_supports_edit_toggle_and_soft_delete(admin_client):
    created = admin_client.post(
        "/api/admins/stock/variance-codes/",
        data=json.dumps({
            "code": "damage_ui",
            "name": "Damage from UI",
            "requires_approval": False,
            "is_active": False,
        }),
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    code_id = created.json()["data"]["id"]
    reason = VarianceReasonCode.objects.get(pk=code_id)
    assert reason.code == "DAMAGE_UI"
    assert reason.is_active is False

    updated = admin_client.put(
        f"/api/admins/stock/variance-codes/{code_id}/",
        data=json.dumps({
            "code": "damage_edit",
            "name": "Edited damage",
            "requires_approval": True,
            "is_active": True,
        }),
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    assert updated.json()["data"]["code"]["code"] == "DAMAGE_EDIT"
    assert updated.json()["data"]["code"]["is_active"] is True

    deleted = admin_client.delete(
        f"/api/admins/stock/variance-codes/{code_id}/"
    )
    assert deleted.status_code == 200, deleted.content
    reason.refresh_from_db()
    assert reason.is_deleted is True
    assert reason.is_active is False
    assert admin_client.get(
        f"/api/admins/stock/variance-codes/{code_id}/"
    ).status_code == 404


def test_unit_create_safely_ignores_frontend_is_active(admin_client):
    response = admin_client.post(
        "/api/admins/stock/units/",
        data=json.dumps({
            "name": "Contract gram",
            "short_name": "ct-g",
            "unit_type": "WEIGHT",
            "is_base_unit": True,
            "is_active": False,
        }),
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    unit = StockUnit.objects.get(pk=response.json()["data"]["id"])
    assert unit.is_active is True


def test_location_activate_route_restores_inactive_location(admin_client):
    location = StockLocation.objects.create(
        name="Inactive contract location",
        type=StockLocation.LocationType.STORAGE,
        is_active=False,
    )

    response = admin_client.post(
        f"/api/admins/stock/locations/{location.id}/activate/",
        data="{}",
        content_type="application/json",
    )

    assert response.status_code == 200, response.content
    location.refresh_from_db()
    assert location.is_active is True
    listed = admin_client.get(
        "/api/admins/stock/locations/?include_inactive=true"
    ).json()["data"]["locations"]
    assert any(row["id"] == location.id for row in listed)


def test_existing_purchase_order_receiving_route_calls_service(
    admin_client, admin_user, monkeypatch,
):
    from stock.views import purchase_views

    captured = {}

    def fake_create(*, purchase_order_id, received_by_id, **payload):
        captured.update({
            "purchase_order_id": purchase_order_id,
            "received_by_id": received_by_id,
            "payload": payload,
        })
        return {"success": True, "data": {"id": 99}}, 201

    monkeypatch.setattr(purchase_views.PurchaseReceivingService, "create", fake_create)

    response = admin_client.post(
        "/api/admins/stock/purchase-order/42/receiving/",
        data=json.dumps({"notes": "received"}),
        content_type="application/json",
    )

    assert response.status_code == 201, response.content
    assert captured == {
        "purchase_order_id": 42,
        "received_by_id": admin_user.id,
        "payload": {"notes": "received"},
    }
