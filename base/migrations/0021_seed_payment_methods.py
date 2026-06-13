"""Seed the four built-in payment methods (CASH/UZCARD/HUMO/PAYME) with default
labels, inline SVG icons (24x24, currentColor so the FE can tint them) and
accent colors. Idempotent via get_or_create — admin edits are preserved."""
from django.db import migrations


_SVG = (
    # CASH — banknote
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/>'
    '<circle cx="12" cy="12" r="2.5"/><path d="M6 12h.01M18 12h.01"/></svg>',
    # UZCARD — card
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/>'
    '<path d="M2 10h20"/></svg>',
    # HUMO — card variant
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/>'
    '<path d="M2 10h20M6 15h4"/></svg>',
    # PAYME — wallet
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 '
    '2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 9h18"/><path d="M16 13h.5"/></svg>',
)

DEFAULTS = [
    {'code': 'CASH',   'label': 'Naqd',   'color': '#16a34a', 'sort_order': 1, 'icon': _SVG[0]},
    {'code': 'UZCARD', 'label': 'Uzcard', 'color': '#1e88e5', 'sort_order': 2, 'icon': _SVG[1]},
    {'code': 'HUMO',   'label': 'Humo',   'color': '#00897b', 'sort_order': 3, 'icon': _SVG[2]},
    {'code': 'PAYME',  'label': 'Payme',  'color': '#33b6ff', 'sort_order': 4, 'icon': _SVG[3]},
]


def seed(apps, schema_editor):
    Cfg = apps.get_model('base', 'PaymentMethodConfig')
    for d in DEFAULTS:
        Cfg.objects.get_or_create(code=d['code'], defaults={
            'label': d['label'], 'color': d['color'],
            'sort_order': d['sort_order'], 'icon': d['icon'], 'is_active': True,
        })


def unseed(apps, schema_editor):
    Cfg = apps.get_model('base', 'PaymentMethodConfig')
    Cfg.objects.filter(code__in=[d['code'] for d in DEFAULTS]).delete()


class Migration(migrations.Migration):
    dependencies = [('base', '0020_paymentmethodconfig_order_discount_percent_and_more')]
    operations = [migrations.RunPython(seed, unseed)]
