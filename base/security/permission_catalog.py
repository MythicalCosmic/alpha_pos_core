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
    ('stock.catalog.view', 'View stock catalog', 'Warehouse'),
    ('stock.level.view', 'View stock levels', 'Warehouse'),
    ('stock.batch.view', 'View batches and receiving history', 'Warehouse'),
    ('stock.supplier.view', 'View suppliers and ledger', 'Warehouse'),
    ('stock.purchase.view', 'View purchase orders', 'Warehouse'),
    ('stock.receiving.create', 'Create receiving drafts', 'Warehouse'),
    ('stock.receiving.update_draft', 'Edit assigned receiving drafts', 'Warehouse'),
    ('stock.receiving.complete', 'Complete receiving', 'Warehouse'),
    ('stock.receiving.approve_over', 'Approve over-receipt', 'Warehouse approvals'),
    ('stock.receiving.correct.approve', 'Approve receiving correction', 'Warehouse approvals'),
    ('stock.transfer.view', 'View stock transfers', 'Warehouse'),
    ('stock.transfer.create', 'Create transfer requests', 'Warehouse'),
    ('stock.count.view', 'View stock counts', 'Warehouse'),
    ('stock.count.create', 'Create stock counts', 'Warehouse'),
    ('stock.count.record', 'Record and submit stock counts', 'Warehouse'),
    ('stock.adjustment.request', 'Request stock adjustments', 'Warehouse'),
    ('stock.adjustment.approve', 'Approve stock adjustments', 'Warehouse approvals'),

    ('attendance.view', 'View attendance', 'Operational audit'),
    ('attendance.record', 'Record attendance', 'Operational audit'),
    ('attendance.adjust.request', 'Request attendance adjustment', 'Operational audit'),
    ('attendance.adjust.approve', 'Approve attendance adjustment', 'Operational audit approvals'),
    ('attendance.schedule.manage', 'Manage employee schedules', 'Operational audit approvals'),
    ('discipline.rule.view', 'View disciplinary rules', 'Operational audit'),
    ('discipline.rule.manage', 'Manage disciplinary rules', 'Operational audit approvals'),
    ('discipline.case.create', 'Create disciplinary cases', 'Operational audit'),
    ('discipline.case.view', 'View disciplinary cases', 'Operational audit'),
    ('discipline.case.approve', 'Approve disciplinary cases', 'Operational audit approvals'),
    ('discipline.case.void', 'Void disciplinary cases', 'Operational audit approvals'),
    ('prep.audit.view', 'View preparation audits', 'Operational audit'),
    ('prep.audit.review', 'Review preparation audits', 'Operational audit'),
    ('prep.audit.reopen', 'Reopen preparation audits', 'Operational audit approvals'),

    ('expense.request.create', 'Create expense requests', 'Expense requests'),
    ('expense.request.view_own', 'View own expense requests', 'Expense requests'),
    ('expense.request.view_all', 'View all expense requests', 'Expense request approvals'),
    ('expense.request.approve', 'Approve expense requests', 'Expense request approvals'),
    ('expense.request.pay', 'Pay approved expense requests', 'Expense request approvals'),

    ('hr.view',          'View HR',                'HR'),
    ('hr.manage',        'Manage HR',              'HR'),

    ('reports.view',     'View reports',           'Reports'),
    ('inkassa.manage',   'Manage branch cash collection',   'Reports'),

    ('users.manage',     'Manage users',           'Administration'),
    ('settings.manage',  'Manage settings',        'Administration'),
]

VALID_KEYS = {p[0] for p in PERMISSIONS}

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
    'WAREHOUSE': [
        'stock.catalog.view', 'stock.level.view', 'stock.batch.view',
        'stock.supplier.view', 'stock.purchase.view',
        'stock.receiving.create', 'stock.receiving.update_draft',
        'stock.receiving.complete', 'stock.transfer.view',
        'stock.transfer.create', 'stock.count.view', 'stock.count.create',
        'stock.count.record', 'stock.adjustment.request',
    ],
    'USER': [],
}


def catalog():
    return [{'key': k, 'label': label, 'group': group} for k, label, group in PERMISSIONS]
