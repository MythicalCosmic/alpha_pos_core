"""Structural guardrails for the explicit synchronization registry."""

import pytest


pytestmark = pytest.mark.django_db


def test_every_nonlocal_sync_model_is_registered_exactly_once():
    from django.apps import apps
    from base.models import SyncMixin
    from base.services.sync.config import MODEL_MAP, SYNC_ORDER

    assert len(SYNC_ORDER) == len(set(SYNC_ORDER))
    assert set(SYNC_ORDER) == set(MODEL_MAP)

    expected = {
        model._meta.label_lower
        for model in apps.get_models()
        if issubclass(model, SyncMixin)
        and model is not SyncMixin
        and not getattr(model, '_sync_local_only', False)
    }
    registered = {label.lower() for label in MODEL_MAP.values()}
    assert registered == expected


def test_every_registered_fk_has_a_uuid_mapping():
    from django.apps import apps
    from base.services.sync.config import FK_UUID_MAPPINGS, MODEL_MAP

    mappings = list(FK_UUID_MAPPINGS.values())
    missing = []
    for label in MODEL_MAP.values():
        model = apps.get_model(label)
        for field in model._meta.fields:
            if not field.is_relation or not (field.many_to_one or field.one_to_one):
                continue
            remote = field.related_model
            found = any(
                app_label.lower() == remote._meta.app_label.lower()
                and model_name.lower() == remote.__name__.lower()
                and local_field == field.name
                for app_label, model_name, local_field in mappings
            )
            if not found:
                missing.append(
                    f'{model._meta.label}.{field.name} -> {remote._meta.label}'
                )

    assert not missing, 'FKs without UUID sync mapping: ' + ', '.join(missing)


def test_branch_owned_financial_children_follow_registered_parent_order():
    """Money children must pull after their authoritative branch parent."""
    from django.apps import apps
    from base.services.sync.config import MODEL_MAP, SYNC_ORDER

    parent_fields = {
        'base.OrderItem': ('order', 'base.Order'),
        'base.OrderPayment': ('order', 'base.Order'),
        'base.OrderRefund': ('order', 'base.Order'),
        'discounts.OrderDiscount': ('order', 'base.Order'),
        'discounts.DiscountUsage': ('order', 'base.Order'),
        'base.CashReconciliation': ('shift', 'base.Shift'),
        'cashbox.ShiftPaymentTotal': ('shift', 'base.Shift'),
        'cashbox.CashboxExpense': ('shift', 'base.Shift'),
    }
    registry_key = {
        label.lower(): key for key, label in MODEL_MAP.items()
    }
    position = {key: index for index, key in enumerate(SYNC_ORDER)}

    for child_label, (field_name, parent_label) in parent_fields.items():
        child = apps.get_model(child_label)
        parent = apps.get_model(parent_label)
        field = child._meta.get_field(field_name)

        assert field.related_model is parent
        assert not field.null
        assert getattr(child, 'SYNC_PULL_SCOPE', 'branch') == 'branch'
        assert getattr(parent, 'SYNC_PULL_SCOPE', 'branch') == 'branch'

        child_key = registry_key[child._meta.label_lower]
        parent_key = registry_key[parent._meta.label_lower]
        assert position[parent_key] < position[child_key], (
            parent_key, child_key,
        )
