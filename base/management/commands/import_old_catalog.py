"""Seed categories + products from the previous (main_*) database.

The data was extracted once from old_db/db.sqlite3 into `_old_catalog.json`
(same directory) — 15 categories + 316 products. Run this ON THE CLOUD so the
records are created with branch_id = settings.BRANCH_ID ('cloud') and synced_at
set, which puts them in the /changes feed so every till pulls them down.

Safe + idempotent:
  * Categories matched by slug; products matched by (name, category).
  * Default: only CREATE what's missing — existing rows are left untouched.
  * --update: also refresh existing rows' fields (price, description, colors…).
  * --dry-run: run the whole thing inside a transaction and roll back (accurate
    counts, nothing persisted).

    python manage.py import_old_catalog --dry-run
    python manage.py import_old_catalog
    python manage.py import_old_catalog --update
"""
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from base.models import Category, Product

DATA_FILE = Path(__file__).resolve().parent / '_old_catalog.json'


def _price(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = "Seed categories + products from the old database (_old_catalog.json)."

    def add_arguments(self, parser):
        parser.add_argument('--update', action='store_true',
                            help='Also update fields of existing categories/products.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Run inside a transaction and roll back (no writes).')

    @transaction.atomic
    def handle(self, *args, **options):
        update = options['update']
        dry = options['dry_run']
        data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        cats = data['categories']
        prods = data['products']
        self.stdout.write(f'Loaded {len(cats)} categories, {len(prods)} products'
                          + ('  (DRY RUN)' if dry else ''))

        # --- Categories (matched by slug) ---------------------------------
        slug_to_cat = {}
        c_created = c_updated = c_skipped = 0
        for c in cats:
            slug = (c.get('slug') or '').strip()
            existing = (Category.objects.filter(slug=slug, is_deleted=False).first()
                        if slug else None)
            if existing:
                slug_to_cat[slug] = existing
                if update:
                    existing.name = c['name']
                    existing.description = c.get('description')
                    existing.sort_order = c.get('sort_order', 0)
                    existing.status = c.get('status', 'ACTIVE')
                    existing.colors = c.get('colors', [])
                    existing.save()
                    c_updated += 1
                else:
                    c_skipped += 1
                continue
            obj = Category(
                name=c['name'], slug=slug, description=c.get('description'),
                sort_order=c.get('sort_order', 0), status=c.get('status', 'ACTIVE'),
                colors=c.get('colors', []),
            )
            obj.save()
            slug_to_cat[slug] = obj
            c_created += 1

        # --- Products (matched by name + category) ------------------------
        p_created = p_updated = p_skipped = p_bad = 0
        for p in prods:
            cat = slug_to_cat.get((p.get('category_slug') or '').strip())
            price = _price(p.get('price'))
            if cat is None or price is None:
                p_bad += 1
                self.stdout.write(self.style.WARNING(
                    f"  skip product {p.get('name')!r}: "
                    f"{'no category' if cat is None else 'bad price'}"))
                continue
            existing = Product.objects.filter(
                name=p['name'], category=cat, is_deleted=False).first()
            if existing:
                if update:
                    existing.price = price
                    existing.description = p.get('description')
                    existing.colors = p.get('colors', [])
                    existing.save()
                    p_updated += 1
                else:
                    p_skipped += 1
                continue
            Product(
                name=p['name'], description=p.get('description'), price=price,
                category=cat, colors=p.get('colors', []),
            ).save()
            p_created += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nCategories: +{c_created} created, ~{c_updated} updated, ={c_skipped} skipped\n'
            f'Products:   +{p_created} created, ~{p_updated} updated, '
            f'={p_skipped} skipped, !{p_bad} bad'))

        if dry:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING('DRY RUN — rolled back, nothing written.'))
        else:
            self.stdout.write(self.style.SUCCESS('Committed.'))
