from django.db import migrations


MANAGER_PERMISSIONS = [
    'money.control.view',
    'money.control.reconcile',
    'stock.inventory_control.view',
    'stock.supplier.view',
    'stock.supplier.balance.view',
    'stock.supplier.pay',
    'stock.purchase.view',
    'expense.category.view',
    'expense.category.manage',
    'expense.request.create',
    'expense.request.view_own',
    'expense.request.view_all',
    'expense.request.approve',
    'expense.request.pay',
    'treasury.account.view',
    'treasury.transfer',
]

WAREHOUSE_PERMISSIONS = [
    'stock.supplier.balance.view',
]


def add_permissions(apps, schema_editor):
    RolePermission = apps.get_model('base', 'RolePermission')
    User = apps.get_model('base', 'User')
    for role, additions in [
        ('MANAGER', MANAGER_PERMISSIONS),
        ('WAREHOUSE', WAREHOUSE_PERMISSIONS),
    ]:
        template, _ = RolePermission.objects.get_or_create(
            role=role,
            defaults={'permissions': []},
        )
        permissions = template.permissions if isinstance(template.permissions, list) else []
        template.permissions = list(dict.fromkeys([*permissions, *additions]))
        template.save(update_fields=['permissions', 'updated_at'])
        for user in User.objects.filter(role=role, is_deleted=False).iterator():
            current = user.permissions if isinstance(user.permissions, list) else []
            User.objects.filter(pk=user.pk).update(
                permissions=list(dict.fromkeys([*current, *additions])),
            )
    admin_template, _ = RolePermission.objects.get_or_create(
        role='ADMIN',
        defaults={'permissions': ['*']},
    )
    if '*' not in (admin_template.permissions or []):
        admin_template.permissions = ['*']
        admin_template.save(update_fields=['permissions', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('base', '0059_alter_idempotencykey_scope'),
        ('hr', '0012_backfill_legacy_treasury_expenses'),
    ]

    operations = [
        migrations.RunPython(add_permissions, migrations.RunPython.noop),
    ]
