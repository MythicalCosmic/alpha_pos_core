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


def test_registered_models_follow_all_cross_model_fk_dependencies():
    """Every sync child must be sent/pulled after its registered parent."""
    from django.apps import apps
    from base.models import SyncMixin
    from base.services.sync.config import MODEL_MAP, SYNC_ORDER

    registry_key = {
        label.lower(): key for key, label in MODEL_MAP.items()
    }
    position = {key: index for index, key in enumerate(SYNC_ORDER)}
    violations = []

    for child_label in MODEL_MAP.values():
        child = apps.get_model(child_label)
        child_key = registry_key[child._meta.label_lower]
        for field in child._meta.concrete_fields:
            parent = getattr(field, 'related_model', None)
            if (
                parent is None
                or parent is child  # self-references cannot be topologically sorted
                or not issubclass(parent, SyncMixin)
                or getattr(parent, '_sync_local_only', False)
            ):
                continue
            parent_key = registry_key.get(parent._meta.label_lower)
            if parent_key is None:
                violations.append(
                    f'{child_key}.{field.name} -> unregistered '
                    f'{parent._meta.label_lower}'
                )
            elif position[parent_key] >= position[child_key]:
                violations.append(
                    f'{child_key}.{field.name} -> {parent_key}'
                )

    assert not violations, 'Out-of-order sync FKs: ' + ', '.join(violations)
