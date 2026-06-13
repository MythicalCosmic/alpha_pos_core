import getpass
from django.core.management.base import BaseCommand
from django.conf import settings
from base.models import User
from base.security.hashing import hash_password


class Command(BaseCommand):
    help = 'Create a new user interactively'

    def _prompt(self, label, default=None, required=True):
        suffix = f' [{default}]' if default else ''
        while True:
            value = input(f'  {label}{suffix}: ').strip()
            if not value and default:
                return default
            if value:
                return value
            if not required:
                return ''
            self.stderr.write(self.style.ERROR(f'    {label} is required'))

    def _prompt_password(self):
        while True:
            pw = getpass.getpass('  Password: ')
            if len(pw) < 6:
                self.stderr.write(self.style.ERROR('    Password must be at least 6 characters'))
                continue
            confirm = getpass.getpass('  Confirm password: ')
            if pw != confirm:
                self.stderr.write(self.style.ERROR('    Passwords do not match'))
                continue
            return pw

    def _pick_role(self):
        roles = list(User.RoleChoices.choices)
        self.stdout.write('\n  Select role:')
        for i, (value, label) in enumerate(roles, 1):
            marker = '*' if value == 'USER' else ' '
            self.stdout.write(f'    {i}. {label:<12} [{marker}]')
        self.stdout.write('')

        while True:
            choice = input('  Enter number [1]: ').strip()
            if not choice:
                return roles[0][0]
            try:
                idx = int(choice)
                if 1 <= idx <= len(roles):
                    selected = roles[idx - 1]
                    self.stdout.write(self.style.SUCCESS(f'    -> {selected[1]}'))
                    return selected[0]
            except ValueError:
                pass
            self.stderr.write(self.style.ERROR(f'    Pick 1-{len(roles)}'))

    def _pick_status(self):
        statuses = list(User.UserStatus.choices)
        self.stdout.write('\n  Select status:')
        for i, (value, label) in enumerate(statuses, 1):
            marker = '*' if value == 'ACTIVE' else ' '
            self.stdout.write(f'    {i}. {label:<12} [{marker}]')
        self.stdout.write('')

        while True:
            choice = input('  Enter number [1]: ').strip()
            if not choice:
                return statuses[0][0]
            try:
                idx = int(choice)
                if 1 <= idx <= len(statuses):
                    selected = statuses[idx - 1]
                    self.stdout.write(self.style.SUCCESS(f'    -> {selected[1]}'))
                    return selected[0]
            except ValueError:
                pass
            self.stderr.write(self.style.ERROR(f'    Pick 1-{len(statuses)}'))

    def handle(self, *args, **options):
        branch_id = getattr(settings, 'BRANCH_ID', '')

        self.stdout.write(self.style.HTTP_INFO(f'\n  Create New User (branch: {branch_id})'))
        self.stdout.write('  ' + '-' * 40 + '\n')

        first_name = self._prompt('First name')
        last_name = self._prompt('Last name')

        while True:
            email = self._prompt('Email')
            if User.objects.filter(email=email).exists():
                self.stderr.write(self.style.ERROR(f'    Email "{email}" already taken'))
                continue
            break

        password = self._prompt_password()
        role = self._pick_role()
        status = self._pick_status()

        self.stdout.write('\n  ' + '-' * 40)
        self.stdout.write(f'  Name:     {first_name} {last_name}')
        self.stdout.write(f'  Email:    {email}')
        self.stdout.write(f'  Role:     {role}')
        self.stdout.write(f'  Status:   {status}')
        self.stdout.write(f'  Branch:   {branch_id}')
        self.stdout.write('  ' + '-' * 40 + '\n')

        confirm = input('  Create this user? [Y/n]: ').strip().lower()
        if confirm and confirm != 'y':
            self.stdout.write(self.style.WARNING('\n  Cancelled.\n'))
            return

        user = User.objects.create(
            first_name=first_name,
            last_name=last_name,
            email=email,
            password=hash_password(password),
            role=role,
            status=status,
            branch_id=branch_id,
        )

        self.stdout.write(self.style.SUCCESS(f'\n  User created! (id={user.id}, uuid={user.uuid})\n'))
