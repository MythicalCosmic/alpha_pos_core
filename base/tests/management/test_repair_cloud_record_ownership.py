from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


pytestmark = pytest.mark.django_db


def _cloud(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'


def _user(branch_id='branch1'):
    from base.models import User

    return User.objects.create(
        first_name='Cloud',
        last_name='Owned',
        email='cloud-owned@example.com',
        password='unused',
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id=branch_id,
    )


def test_ownership_repair_is_dry_run_by_default(settings):
    _cloud(settings)
    user = _user()
    original_version = user.sync_version
    out = StringIO()

    call_command('repair_cloud_record_ownership', 'user', stdout=out)

    user.refresh_from_db()
    assert user.branch_id == 'branch1'
    assert user.sync_version == original_version
    assert '(DRY RUN)' in out.getvalue()
    assert 'Would repair 1 record(s)' in out.getvalue()


def test_apply_reowns_only_selected_models_and_publishes(
    settings,
    django_capture_on_commit_callbacks,
):
    from base.models import Category

    _cloud(settings)
    user = _user()
    category = Category.objects.create(
        name='Branch catalog',
        slug='branch-catalog',
        branch_id='branch1',
    )
    original_version = user.sync_version

    with django_capture_on_commit_callbacks(execute=True):
        call_command(
            'repair_cloud_record_ownership',
            'user',
            '--apply',
            stdout=StringIO(),
        )

    user.refresh_from_db()
    category.refresh_from_db()
    assert user.branch_id == 'cloud'
    assert user.sync_version == original_version + 1
    assert user.synced_at is not None
    assert category.branch_id == 'branch1'


@pytest.mark.parametrize(
    ('deployment_mode', 'branch_id', 'message'),
    [
        ('local', 'branch1', 'restricted to cloud mode'),
        ('cloud', '', 'BRANCH_ID must not be empty'),
    ],
)
def test_ownership_repair_rejects_unsafe_runtime(
    settings,
    deployment_mode,
    branch_id,
    message,
):
    settings.DEPLOYMENT_MODE = deployment_mode
    settings.BRANCH_ID = branch_id

    with pytest.raises(CommandError, match=message):
        call_command('repair_cloud_record_ownership', stdout=StringIO())


def test_legacy_ownership_name_uses_canonical_command():
    from base.management.commands.reown_for_cloud import Command as LegacyCommand
    from base.management.commands.repair_cloud_record_ownership import (
        Command as CanonicalCommand,
    )

    assert LegacyCommand is CanonicalCommand
