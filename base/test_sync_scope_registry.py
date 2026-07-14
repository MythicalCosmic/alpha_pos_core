import pytest


pytestmark = pytest.mark.django_db


GLOBAL_MODELS = {
    # Same-company identity + menu/shift configuration.
    'user', 'category', 'product', 'shifttemplate',
    # Inventory/recipe catalogs; quantities and transactions remain branch.
    'stockunit', 'stockcategory', 'variancereasoncode',
    # Shared HR/discount/cashbox catalogs.
    'department', 'expensecategory', 'leavetype',
    'discounttype', 'discount', 'cashboxexpensecategory',
}


def test_every_sync_model_has_the_intended_pull_scope():
    from base.services.sync.config import SYNC_ORDER, get_all_models

    models = get_all_models()
    assert GLOBAL_MODELS <= set(SYNC_ORDER)
    actual_global = {
        name for name in SYNC_ORDER
        if name in models
        and getattr(models[name], 'SYNC_PULL_SCOPE', 'branch') == 'global'
    }
    assert actual_global == GLOBAL_MODELS

    for name in SYNC_ORDER:
        model = models.get(name)
        if model is None:
            continue
        scope = getattr(model, 'SYNC_PULL_SCOPE', None)
        assert scope in {'branch', 'global'}, (name, scope)
        if name not in GLOBAL_MODELS:
            assert scope == 'branch', name


def test_global_models_deny_every_branch_mutable_field_and_soft_delete():
    """Future catalog fields must be protected without another hand audit."""
    from base.services.sync.config import get_all_models

    models = get_all_models()
    for name in GLOBAL_MODELS:
        model = models[name]
        denied = model._effective_denylist(mode='cloud')
        assert 'is_deleted' in denied, name
        for field in model._meta.concrete_fields:
            assert field.name in denied, (name, field.name)
            assert field.attname in denied, (name, field.attname)


def test_global_pull_graph_never_depends_on_a_branch_scoped_parent():
    """A global child whose FK parent is branch-only defers forever elsewhere."""
    from base.models import SyncMixin
    from base.services.sync.config import get_all_models

    for name, model in get_all_models().items():
        if getattr(model, 'SYNC_PULL_SCOPE', 'branch') != 'global':
            continue
        for field in model._meta.concrete_fields:
            parent = getattr(field, 'related_model', None)
            if parent is None or not issubclass(parent, SyncMixin):
                continue
            assert getattr(parent, 'SYNC_PULL_SCOPE', 'branch') == 'global', (
                f'{name}.{field.name} is global but depends on branch-scoped '
                f'{parent._meta.label_lower}'
            )
