"""Activate this install's license WITHOUT a control center.

The normal path to ACTIVE is the setup wizard registering against the
vendor control center (LICENSE_CONTROL_CENTER_URL). When the vendor runs
the POS themselves, or the control center isn't stood up yet, that path
is unavailable and the kill switch (licensing/middleware.py) 503s every
business endpoint forever.

This command sets the License singleton to ACTIVE directly. The kill
switch passes on status=ACTIVE as long as `expires_at` hasn't passed and
the offline-grace window (which only exists once a heartbeat has landed)
isn't exceeded — so an activation with no expiry and the heartbeat daemon
disabled runs indefinitely offline.

Usage:
    python manage.py activate_offline --email you@example.com --org "My Cafe"
    python manage.py activate_offline --expires 2027-01-01      # optional end date
    python manage.py activate_offline --deactivate              # back to UNREGISTERED

IMPORTANT: when relying on this, set LICENSE_HEARTBEAT_DISABLED=1 (or omit
the daemon) so a failing heartbeat against a non-existent control center
doesn't churn logs. This is a vendor/operator action; the License model is
host-local and never syncs to other branches.
"""
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError

from licensing.models import License, LicenseEvent


class Command(BaseCommand):
    help = "Activate the local license offline (no control center required)."

    def add_arguments(self, parser):
        parser.add_argument('--email', default='', help='Operator email to stamp on the license.')
        parser.add_argument('--org', default='', help='Organization name to stamp on the license.')
        parser.add_argument(
            '--expires', default='',
            help='Optional ISO date (YYYY-MM-DD) after which the license self-expires. '
                 'Omit for a perpetual offline license.',
        )
        parser.add_argument(
            '--deactivate', action='store_true',
            help='Reset the license back to UNREGISTERED (re-enables the kill switch).',
        )
        parser.add_argument(
            '--perpetual', action='store_true',
            help='Explicitly confirm a NEVER-EXPIRING offline license. Required '
                 'when --expires is omitted, so a perpetual, un-revocable '
                 'activation can never be created by accident.',
        )

    def handle(self, *args, **opts):
        lic = License.load()

        if opts['deactivate']:
            lic.status = License.Status.UNREGISTERED
            lic.expires_at = None
            lic.registered_at = None
            lic.save()
            LicenseEvent.objects.create(
                action=LicenseEvent.Action.STATUS_CHANGED,
                detail={'to': 'UNREGISTERED', 'via': 'activate_offline --deactivate'},
            )
            self.stdout.write(self.style.WARNING(
                'License reset to UNREGISTERED — the kill switch will now block '
                'all business endpoints until re-activated or registered.'
            ))
            return

        expires_at = None
        if opts['expires']:
            try:
                parsed = datetime.strptime(opts['expires'], '%Y-%m-%d')
            except ValueError:
                raise CommandError('--expires must be an ISO date like 2027-01-01')
            expires_at = parsed.replace(tzinfo=dt_timezone.utc)
        elif not opts['perpetual']:
            # A no-expiry offline activation never expires and (with the daemon
            # disabled) has no grace clock — it can only be undone by shell
            # access. Refuse to create one without explicit confirmation so it
            # can't happen by accident or be slipped in unnoticed.
            raise CommandError(
                'Refusing to create a perpetual, un-revocable license without '
                'confirmation. Pass --expires YYYY-MM-DD for a bounded license, '
                'or --perpetual to explicitly confirm a never-expiring one.'
            )

        lic.status = License.Status.ACTIVE
        lic.email = opts['email'] or lic.email
        lic.org_name = opts['org'] or lic.org_name
        lic.expires_at = expires_at
        if lic.registered_at is None:
            from django.utils import timezone
            lic.registered_at = timezone.now()
        lic.save()  # busts the middleware's state cache

        LicenseEvent.objects.create(
            action=LicenseEvent.Action.STATUS_CHANGED,
            detail={
                'to': 'ACTIVE',
                'via': 'activate_offline',
                'expires_at': expires_at.isoformat() if expires_at else None,
            },
        )

        self.stdout.write(self.style.SUCCESS(
            'License activated offline (status=ACTIVE'
            + (f', expires {opts["expires"]}' if expires_at else ', perpetual')
            + '). Business endpoints will now respond.'
        ))
        if not expires_at:
            self.stdout.write(self.style.WARNING(
                'WARNING: this is a PERPETUAL, never-expiring license with no '
                'remote kill path. Revoke only via "activate_offline --deactivate".'
            ))
        self.stdout.write(
            'Reminder: set LICENSE_HEARTBEAT_DISABLED=1 so the daemon does not '
            'retry a non-existent control center.'
        )
