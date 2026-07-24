"""The catalog of assignable permission keys, grouped for the role editor.

Enforcement is per-user (User.permissions, checked by @permission_required and
the FE's gating). This catalog is the source of truth the Settings → Roles
editor lists, plus the default permission set each role ships with. The six
keys currently enforced by @permission_required server-side
(category.update/delete, product.update/delete, order.update/stats) are a
subset; the rest gate frontend affordances.
"""

# (key, label, group)
PERMISSIONS = [
    ('order.create',     'Create orders',          'Orders'),
    ('order.update',     'Edit orders',            'Orders'),
    ('order.pay',        'Take payment',           'Orders'),
    ('order.cancel',     'Cancel orders',          'Orders'),
    ('order.stats',      'View order stats',       'Orders'),
    ('discount.apply',   'Apply discounts',        'Orders'),

    ('product.create',   'Create products',        'Menu'),
    ('product.update',   'Edit products',          'Menu'),
    ('product.delete',   'Delete products',        'Menu'),
    ('category.create',  'Create categories',      'Menu'),
    ('category.update',  'Edit categories',        'Menu'),
    ('category.delete',  'Delete categories',      'Menu'),

    ('stock.view',       'View stock',             'Stock'),
    ('stock.manage',     'Manage stock',           'Stock'),

    ('hr.view',          'View HR',                'HR'),
    ('hr.manage',        'Manage HR',              'HR'),

    ('reports.view',     'View reports',           'Reports'),
    ('inkassa.manage',   'Manage branch cash collection',   'Reports'),

    ('users.manage',     'Manage users',           'Administration'),
    ('settings.manage',  'Manage settings',        'Administration'),
]

VALID_KEYS = {p[0] for p in PERMISSIONS}

# Default permission set per role. ADMIN uses the '*' wildcard (bypasses every
# check). Roles are the User.RoleChoices values.
DEFAULT_ROLE_PERMISSIONS = {
    'ADMIN': ['*'],
    'MANAGER': [
        'order.create', 'order.update', 'order.pay', 'order.cancel', 'order.stats',
        'discount.apply', 'product.create', 'product.update', 'product.delete',
        'category.create', 'category.update', 'category.delete',
        'stock.view', 'stock.manage', 'hr.view', 'reports.view', 'inkassa.manage',
    ],
    'CASHIER': [
        'order.create', 'order.update', 'order.pay', 'discount.apply',
    ],
    'WAITER': [
        'order.create', 'order.update',
    ],
    # Kitchen label, created without a password and can't log in (no picker
    # entry) — so it holds no POS permissions.
    'CHEF': [],
    'USER': [],
}


def catalog():
    return [{'key': k, 'label': label, 'group': group} for k, label, group in PERMISSIONS]
