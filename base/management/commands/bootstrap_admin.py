"""First-run admin bootstrap.

Idempotent and non-interactive. Used by `run.py` and the Windows
`install.bat` so a fresh install lands on a working login screen
without making the user run `createuser` interactively.

Behavior:
- If any User row exists, this is a no-op (existing installs are not
  touched).
- Otherwise, create one ADMIN user with the email/password supplied
  via flags or env vars. If neither is supplied, generate a random
  password and PRINT it prominently — the operator must record it.

Why not Django's `createsuperuser`: this project's User is a custom
model (no is_superuser / is_staff) and credentials go through the
project's own hashing helper.
"""
import secrets
import string
import sys

from django.core.management.base import BaseCommand

from base.models import User
from base.security.hashing import hash_password


DEFAULT_EMAIL = 'admin@local'


def _generate_password(length: int = 16) -> str:
    """Return a URL-safe-ish random password. Excludes ambiguous chars
    (0/O, 1/l/I) so the operator can read it off the terminal once."""
    alphabet = string.ascii_letters + string.digits
    for bad in '0O1lI':
        alphabet = alphabet.replace(bad, '')
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class Command(BaseCommand):
    help = 'Create a first ADMIN user if no users exist (idempotent, non-interactive).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            default=None,
            help=f'Admin email. Defaults to ALPHA_POS_ADMIN_EMAIL env or "{DEFAULT_EMAIL}".',
        )
        parser.add_argument(
            '--password',
            default=None,
            help='Admin password. Defaults to ALPHA_POS_ADMIN_PASSWORD env, or a random one.',
        )
        parser.add_argument(
            '--first-name',
            default='Admin',
        )
        parser.add_argument(
            '--last-name',
            default='User',
        )

    def handle(self, *args, **options):
        import os

        if User.objects.exists():
            self.stdout.write(
                'bootstrap_admin: users already exist — skipping. '
                f'(count={User.objects.count()})'
            )
            return

        email = (
            options.get('email')
            or os.environ.get('ALPHA_POS_ADMIN_EMAIL')
            or DEFAULT_EMAIL
        ).strip().lower()

        password = (
            options.get('password')
            or os.environ.get('ALPHA_POS_ADMIN_PASSWORD')
            or None
        )
        generated = False
        if not password:
            password = _generate_password()
            generated = True

        user = User.objects.create(
            first_name=options.get('first_name') or 'Admin',
            last_name=options.get('last_name') or 'User',
            email=email,
            password=hash_password(password),
            role=User.RoleChoices.ADMIN,
            status=User.UserStatus.ACTIVE,
        )

        # Print credentials prominently — banner with stderr so it survives
        # in service logs even when stdout is piped to a log rotator.
        banner = '=' * 64
        msg = (
            f'\n{banner}\n'
            f'  ALPHA POS — first admin created\n'
            f'{banner}\n'
            f'  Email:    {email}\n'
            f'  Password: {password}'
            f'{"  (auto-generated — record it NOW)" if generated else ""}\n'
            f'  Role:     ADMIN\n'
            f'  User ID:  {user.id}\n'
            f'{banner}\n'
        )
        # Use stderr so it's visible even when run.py pipes stdout to a logfile.
        sys.stderr.write(msg)
        sys.stderr.flush()
