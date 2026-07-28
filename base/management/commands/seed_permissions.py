from django.core.management.base import BaseCommand

from base.models import User
from base.security.permission_catalog import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSIONS,
)


def _role_permissions(role):
    return tuple(DEFAULT_ROLE_PERMISSIONS[role])


_MANAGER_PERMISSIONS = _role_permissions('MANAGER')

ROLE_PRESETS = {
    'full': _role_permissions('ADMIN'),
    **{
        role.lower(): _role_permissions(role)
        for role in DEFAULT_ROLE_PERMISSIONS
    },
    'viewer': tuple(
        permission
        for permission in _MANAGER_PERMISSIONS
        if permission == 'order.stats' or permission.endswith('.view')
    ),
    'order_manager': tuple(
        permission
        for permission in _MANAGER_PERMISSIONS
        if permission.startswith(('order.', 'discount.'))
    ),
    'product_manager': tuple(
        permission
        for permission in _MANAGER_PERMISSIONS
        if permission.startswith(('product.', 'category.'))
    ),
    'hr_manager': tuple(
        permission
        for permission in _MANAGER_PERMISSIONS
        if permission.startswith('hr.')
    ),
}


class Command(BaseCommand):
    help = 'Seed permissions for admin users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--preset',
            type=str,
            choices=list(ROLE_PRESETS.keys()),
            help='Permission preset to apply',
        )
        parser.add_argument(
            '--email',
            type=str,
            help='Apply to specific user by email',
        )
        parser.add_argument(
            '--all-admins',
            action='store_true',
            help='Apply to all admin users',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            dest='list_perms',
            help='List all available permissions and presets',
        )

    def handle(self, *args, **options):
        if options['list_perms']:
            self._list_permissions()
            return

        preset = options.get('preset')
        email = options.get('email')
        all_admins = options.get('all_admins')

        if not preset:
            self.stderr.write(self.style.ERROR('  --preset is required'))
            return

        permissions = list(ROLE_PRESETS[preset])

        if email:
            users = User.objects.filter(email=email, role='ADMIN')
            if not users.exists():
                self.stderr.write(self.style.ERROR(f'  Admin user with email {email} not found'))
                return
        elif all_admins:
            users = User.objects.filter(role='ADMIN')
        else:
            self.stderr.write(self.style.ERROR('  Specify --email or --all-admins'))
            return

        count = 0
        for user in users:
            user.permissions = permissions
            user.save(update_fields=['permissions'])
            count += 1
            self.stdout.write(f'  {user.email} -> {preset} ({len(permissions)} permissions)')

        self.stdout.write(self.style.SUCCESS(f'\n  Done! Updated {count} admin(s) with "{preset}" preset'))

    def _list_permissions(self):
        self.stdout.write('\n  Available permissions:')
        for key, label, group in PERMISSIONS:
            self.stdout.write(f'    - {key} ({group}: {label})')

        self.stdout.write('\n  Presets:')
        for name, perms in ROLE_PRESETS.items():
            self.stdout.write(f'    {name}: {", ".join(perms)}')
        self.stdout.write('')
