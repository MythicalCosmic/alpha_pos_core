from django.core.management.base import BaseCommand
from base.models import User


ALL_PERMISSIONS = [
    'category.create',
    'category.update',
    'category.delete',

    'product.create',
    'product.update',
    'product.delete',

    'order.create',
    'order.update',
    'order.delete',
    'order.stats',

    'hr.departments.create',
    'hr.departments.update',
    'hr.departments.delete',

    'hr.employees.create',
    'hr.employees.update',
    'hr.employees.delete',

    'hr.expenses.create',
    'hr.expenses.update',
    'hr.expenses.delete',
    'hr.expenses.approve',
    'hr.expenses.pay',

    'hr.salary.create',
    'hr.salary.update',
    'hr.salary.delete',
    'hr.salary.approve',
    'hr.salary.pay',
    'hr.salary.generate',

    'hr.cash.deposit',
    'hr.cash.withdraw',

    'hr.contracts.create',
    'hr.contracts.update',
    'hr.contracts.delete',
    'hr.contracts.activate',
    'hr.contracts.terminate',
    'hr.contracts.renew',

    'hr.leave.types.create',
    'hr.leave.types.update',
    'hr.leave.types.delete',
    'hr.leave.create',
    'hr.leave.approve',
    'hr.leave.cancel',

    'hr.attendance.create',
    'hr.attendance.update',
    'hr.attendance.reports',

    'hr.documents.create',
    'hr.documents.update',
    'hr.documents.delete',
    'hr.documents.verify',

    'hr.reviews.create',
    'hr.reviews.update',
    'hr.reviews.delete',

    'hr.goals.create',
    'hr.goals.update',
    'hr.goals.delete',

    'hr.events.create',
    'hr.events.delete',
]

ROLE_PRESETS = {
    'full': ['*'],
    'manager': [
        'category.create', 'category.update',
        'product.create', 'product.update',
        'order.create', 'order.update', 'order.stats',
    ],
    'viewer': [
        'order.stats',
    ],
    'order_manager': [
        'order.create', 'order.update', 'order.delete', 'order.stats',
    ],
    'product_manager': [
        'product.create', 'product.update', 'product.delete',
        'category.create', 'category.update', 'category.delete',
    ],
    'hr_manager': [
        'hr.departments.create', 'hr.departments.update', 'hr.departments.delete',
        'hr.employees.create', 'hr.employees.update', 'hr.employees.delete',
        'hr.expenses.create', 'hr.expenses.update', 'hr.expenses.delete',
        'hr.expenses.approve', 'hr.expenses.pay',
        'hr.salary.create', 'hr.salary.update', 'hr.salary.delete',
        'hr.salary.approve', 'hr.salary.pay', 'hr.salary.generate',
        'hr.cash.deposit', 'hr.cash.withdraw',
        'hr.contracts.create', 'hr.contracts.update', 'hr.contracts.delete',
        'hr.contracts.activate', 'hr.contracts.terminate', 'hr.contracts.renew',
        'hr.leave.types.create', 'hr.leave.types.update', 'hr.leave.types.delete',
        'hr.leave.create', 'hr.leave.approve', 'hr.leave.cancel',
        'hr.attendance.create', 'hr.attendance.update', 'hr.attendance.reports',
        'hr.documents.create', 'hr.documents.update', 'hr.documents.delete', 'hr.documents.verify',
        'hr.reviews.create', 'hr.reviews.update', 'hr.reviews.delete',
        'hr.goals.create', 'hr.goals.update', 'hr.goals.delete',
        'hr.events.create', 'hr.events.delete',
    ],
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

        permissions = ROLE_PRESETS[preset]

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
        for perm in ALL_PERMISSIONS:
            self.stdout.write(f'    - {perm}')

        self.stdout.write('\n  Presets:')
        for name, perms in ROLE_PRESETS.items():
            self.stdout.write(f'    {name}: {", ".join(perms)}')
        self.stdout.write('')
