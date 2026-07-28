
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
    ('inkassa.manage',   'Manage cash register',   'Reports'),

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
    'CHEF': [],
    'USER': [],
}


def catalog():
    return [{'key': k, 'label': label, 'group': group} for k, label, group in PERMISSIONS]
