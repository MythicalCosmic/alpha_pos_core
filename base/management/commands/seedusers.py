import random
import string
from django.core.management.base import BaseCommand
from django.conf import settings
from base.models import User
from base.security.hashing import hash_password


FIRST_NAMES = [
    'Alex', 'Jordan', 'Sam', 'Morgan', 'Casey', 'Riley', 'Quinn', 'Avery',
    'Charlie', 'Dakota', 'Emery', 'Finley', 'Harper', 'Jaden', 'Kai',
    'Logan', 'Marley', 'Noah', 'Oakley', 'Peyton', 'Reese', 'Sage',
    'Taylor', 'Val', 'Winter', 'Zara', 'Blake', 'Drew', 'Ellis', 'Flynn',
    'Gray', 'Hollis', 'Indigo', 'Jules', 'Kendall', 'Lane', 'Milan',
    'Nico', 'Onyx', 'Phoenix', 'Raven', 'Shay', 'Tatum', 'Uma', 'Wren',
]

LAST_NAMES = [
    'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller',
    'Davis', 'Rodriguez', 'Martinez', 'Wilson', 'Anderson', 'Taylor',
    'Thomas', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson',
    'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson',
    'Walker', 'Young', 'Allen', 'King', 'Wright', 'Scott', 'Torres',
    'Nguyen', 'Hill', 'Flores', 'Green', 'Adams', 'Nelson', 'Baker',
    'Hall', 'Rivera', 'Campbell', 'Mitchell', 'Carter', 'Roberts', 'Kim',
]

DOMAINS = ['mail.com', 'inbox.uz', 'work.co', 'pos.local', 'test.dev']


class Command(BaseCommand):
    help = 'Seed database with random users'

    def add_arguments(self, parser):
        parser.add_argument('count', type=int, help='Number of users to generate')
        parser.add_argument(
            '--role',
            type=str,
            choices=['USER', 'CASHIER', 'ADMIN', 'mixed'],
            default='mixed',
            help='Role for all users or "mixed" for random distribution (default: mixed)',
        )
        parser.add_argument(
            '--password',
            type=str,
            default='password123',
            help='Password for all generated users (default: password123)',
        )

    def handle(self, *args, **options):
        count = options['count']
        role_mode = options['role']
        password = hash_password(options['password'])
        branch_id = getattr(settings, 'BRANCH_ID', '')

        if count < 1:
            self.stderr.write(self.style.ERROR('Count must be at least 1'))
            return

        self.stdout.write(f'\n  Generating {count} users...\n')

        role_weights = {
            'USER': 0.5,
            'CASHIER': 0.4,
            'ADMIN': 0.1,
        }

        created = 0
        skipped = 0
        used_emails = set(User.objects.values_list('email', flat=True))

        for i in range(count):
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)

            base_email = f"{first.lower()}.{last.lower()}"
            domain = random.choice(DOMAINS)
            email = f"{base_email}@{domain}"

            attempt = 0
            while email in used_emails:
                attempt += 1
                suffix = ''.join(random.choices(string.digits, k=2))
                email = f"{base_email}{suffix}@{domain}"
                if attempt > 20:
                    break

            if email in used_emails:
                skipped += 1
                continue

            if role_mode == 'mixed':
                roll = random.random()
                if roll < role_weights['USER']:
                    role = 'USER'
                elif roll < role_weights['USER'] + role_weights['CASHIER']:
                    role = 'CASHIER'
                else:
                    role = 'ADMIN'
            else:
                role = role_mode

            User.objects.create(
                first_name=first,
                last_name=last,
                email=email,
                password=password,
                role=role,
                status='ACTIVE',
                branch_id=branch_id,
            )
            used_emails.add(email)
            created += 1

            bar_width = 30
            filled = int(bar_width * (i + 1) / count)
            bar = '=' * filled + '-' * (bar_width - filled)
            self.stdout.write(f'\r  [{bar}] {i + 1}/{count}', ending='')
            self.stdout.flush()

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'\n  Done! Created {created} users.'))
        if skipped:
            self.stdout.write(self.style.WARNING(f'  Skipped {skipped} (duplicate emails)'))
        self.stdout.write(f'  Password: {options["password"]}')
        self.stdout.write(f'  Branch: {branch_id}\n')
