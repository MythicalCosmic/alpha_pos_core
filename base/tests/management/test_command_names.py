from io import StringIO

import pytest
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError

from base.security.permission_catalog import (
    DEFAULT_ROLE_PERMISSIONS,
    VALID_KEYS,
)


COMMAND_ALIASES = {
    'createuser': 'create_user',
    'genpostman': 'generate_postman_collection',
    'seedusers': 'seed_demo_users',
    'seed_products': 'seed_demo_products',
    'seedpermissions': 'seed_permissions',
}


def test_canonical_commands_and_legacy_aliases_are_discoverable():
    commands = get_commands()

    for legacy_name, canonical_name in COMMAND_ALIASES.items():
        assert commands[legacy_name] == 'base'
        assert commands[canonical_name] == 'base'


@pytest.mark.parametrize(
    ('legacy_module', 'canonical_module'),
    [
        ('createuser', 'create_user'),
        ('genpostman', 'generate_postman_collection'),
        ('seedusers', 'seed_demo_users'),
        ('seed_products', 'seed_demo_products'),
        ('seedpermissions', 'seed_permissions'),
    ],
)
def test_legacy_command_modules_reexport_canonical_command(
    legacy_module,
    canonical_module,
):
    legacy = __import__(
        f'base.management.commands.{legacy_module}',
        fromlist=['Command'],
    )
    canonical = __import__(
        f'base.management.commands.{canonical_module}',
        fromlist=['Command'],
    )

    assert legacy.Command is canonical.Command


@pytest.mark.parametrize(
    ('command_name', 'arguments'),
    [
        ('seed_demo_users', ('1',)),
        (
            'seed_demo_products',
            ('--products', '1', '--categories', '1'),
        ),
    ],
)
def test_demo_seed_commands_refuse_cloud_writes(
    settings,
    command_name,
    arguments,
):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'

    with pytest.raises(CommandError, match='--allow-cloud'):
        call_command(
            command_name,
            *arguments,
            stdout=StringIO(),
            stderr=StringIO(),
        )


def test_demo_seed_cloud_override_is_explicit(settings):
    from base.management.commands._demo_seed import (
        require_demo_seed_permission,
    )

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'

    require_demo_seed_permission({'allow_cloud': True})


def test_permission_presets_come_from_supported_role_catalog():
    from base.management.commands.seed_permissions import ROLE_PRESETS

    for role, permissions in DEFAULT_ROLE_PERMISSIONS.items():
        assert ROLE_PRESETS[role.lower()] == tuple(permissions)

    assert ROLE_PRESETS['full'] == tuple(DEFAULT_ROLE_PERMISSIONS['ADMIN'])
    assert all(
        permission == '*' or permission in VALID_KEYS
        for permissions in ROLE_PRESETS.values()
        for permission in permissions
    )
