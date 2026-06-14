"""Seed demo categories + products — for load / sync stress testing.

    python manage.py seed_products                  # 300 products, 12 categories
    python manage.py seed_products --products 500 --categories 20

Products are created via .save() (NOT bulk_create) so each one enqueues for cloud
sync on a branch / local edition — letting you watch the sync pipeline under load.
"""
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils.text import slugify

CATEGORY_NAMES = [
    'Burgers', 'Pizza', 'Drinks', 'Desserts', 'Salads', 'Sides', 'Coffee',
    'Breakfast', 'Soups', 'Grill', 'Pasta', 'Seafood', 'Vegan', 'Kids', 'Combos',
]
ADJ = ['Classic', 'Spicy', 'Deluxe', 'Royal', 'Smoky', 'Crispy', 'Double', 'Mega',
       'Garden', 'House', 'Chef', 'Golden', 'Fresh', 'Loaded', 'Grilled']
NOUN = ['Burger', 'Pizza', 'Cola', 'Cheesecake', 'Caesar', 'Fries', 'Latte',
        'Omelette', 'Lagman', 'Kebab', 'Carbonara', 'Shrimp', 'Wrap', 'Combo', 'Shake']


class Command(BaseCommand):
    help = 'Seed demo categories + products for load / sync testing.'

    def add_arguments(self, parser):
        parser.add_argument('--products', type=int, default=300,
                            help='How many products to create (default 300).')
        parser.add_argument('--categories', type=int, default=12,
                            help='How many categories to create (default 12).')

    def handle(self, *args, **opts):
        from base.models import Category, Product
        n_cat = max(1, opts['categories'])
        n_prod = max(1, opts['products'])

        cats = []
        for i in range(n_cat):
            base = CATEGORY_NAMES[i % len(CATEGORY_NAMES)]
            name = base if i < len(CATEGORY_NAMES) else f'{base} {i // len(CATEGORY_NAMES) + 1}'
            cats.append(Category.objects.create(
                name=name, slug=f'{slugify(name)}-seed-{i}', sort_order=i,
                status='ACTIVE', description='Seeded demo category.',
            ))
        self.stdout.write(f'  categories: {len(cats)}')

        made = 0
        for i in range(n_prod):
            Product.objects.create(
                name=f'{random.choice(ADJ)} {random.choice(NOUN)} #{i + 1}',
                category=cats[i % len(cats)],
                price=Decimal(random.randrange(5000, 150000, 1000)),
                description='Seeded demo product for load/sync testing.',
                is_instant=(random.random() < 0.3),
            )
            made += 1
            if made % 50 == 0:
                self.stdout.write(f'  products: {made}/{n_prod}')

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(cats)} categories + {made} products.'))
