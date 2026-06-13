"""Repair users whose password was stored as PLAINTEXT (e.g. created via the
Django /admin/ page before UserAdmin hashed on save). Treats the current value
as the raw password/PIN and replaces it with a proper Django hash, so
login's check_password() can verify it.

    python manage.py rehash_passwords          # fix all plaintext rows
    python manage.py rehash_passwords --dry-run

On the cloud (mode='cloud') the rehashed password propagates down to branches
on the next pull (User password is accepted on the branch-pull direction).
"""
from django.contrib.auth.hashers import identify_hasher
from django.core.management.base import BaseCommand

from base.models import User
from base.security.hashing import hash_password


class Command(BaseCommand):
    help = "Hash any User.password stored as plaintext."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        fixed = 0
        for user in User.objects.all():
            pw = user.password or ''
            if not pw:
                continue
            try:
                identify_hasher(pw)
                continue  # already a recognized hash — leave it
            except Exception:  # noqa: BLE001 — unrecognized => plaintext
                pass
            self.stdout.write(f'  {"would fix" if dry else "fixing"}: '
                              f'id={user.id} email={user.email}')
            if not dry:
                user.password = hash_password(pw)
                user.save(update_fields=['password', 'synced_at', 'sync_version'])
            fixed += 1
        verb = 'would rehash' if dry else 'rehashed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {fixed} plaintext password(s).'))
