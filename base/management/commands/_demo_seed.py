from django.conf import settings
from django.core.management.base import CommandError


def add_allow_cloud_argument(parser):
    parser.add_argument(
        '--allow-cloud',
        action='store_true',
        help='Permit demo data to be written to a cloud deployment.',
    )


def require_demo_seed_permission(options):
    mode = str(getattr(settings, 'DEPLOYMENT_MODE', '') or '').strip().lower()
    branch_id = str(getattr(settings, 'BRANCH_ID', '') or '').strip().lower()
    if not options.get('allow_cloud') and (mode == 'cloud' or branch_id == 'cloud'):
        raise CommandError(
            'Demo seeding is blocked on cloud deployments. '
            'Pass --allow-cloud only when demo data is explicitly intended.'
        )
