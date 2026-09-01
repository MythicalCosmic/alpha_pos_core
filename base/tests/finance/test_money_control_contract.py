from datetime import datetime, time
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

from base.models import TreasuryAccount, TreasuryTransaction, User
from base.services.money_control_service import (
    MoneyControlService,
    WORKING_CAPITAL_FORMULA,
)
from base.services.treasury_service import _apply, _lock_accounts
from hr.models import Expense, ExpenseCategory
from hr.services.expense_service import ExpenseService
from hr.services.expense_category_service import ExpenseCategoryService
from cashbox.services.expense_service import CashboxCategoryService
from stock.models import (
    StockBatch,
    StockItem,
    StockLevel,
    StockLocation,
    StockUnit,
    Supplier,
    SupplierStockItem,
)
from stock.services.inventory_control_service import InventoryControlService


pytestmark = pytest.mark.django_db


def _user(email, branch='branch1'):
    return User.objects.create(
        first_name=email.split('@')[0],
        last_name='Tester',
        email=email,
        password='!',
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
        permissions=['*'],
        branch_id=branch,
    )


def _credit_treasury(branch, safe, bank, actor):
    accounts = _lock_accounts(['SAFE', 'BANK'], branch)
    _apply(
        accounts['SAFE'], Decimal(safe), TreasuryTransaction.Type.ADJUSTMENT,
        branch_id=branch, performed_by=actor,
    )
    _apply(
        accounts['BANK'], Decimal(bank), TreasuryTransaction.Type.ADJUSTMENT,
        branch_id=branch, performed_by=actor,
    )


def _raw_stock(branch='branch1', quantity='5', cost='100', reorder='2'):
    unit = StockUnit.objects.create(
        name='Kilogram', short_name='kg', unit_type='WEIGHT',
        is_base_unit=True,
    )
    location = StockLocation.objects.create(
        name='Warehouse', type='WAREHOUSE', branch_id=branch,
    )
    item = StockItem.objects.create(
        name='Flour', sku=f'RAW-{uuid4().hex[:8]}', base_unit=unit,
        item_type=StockItem.ItemType.RAW, avg_cost_price=cost,
        reorder_point=reorder, branch_id=branch,
    )
    StockLevel.objects.create(
        stock_item=item, location=location, quantity=quantity,
        branch_id=branch,
    )
    return item, location


def test_inventory_summary_precedes_pagination_and_uses_reorder_point():
    actor = _user('inventory@test.local')
    unit = StockUnit.objects.create(
        name='Piece', short_name='pc', unit_type='COUNT', is_base_unit=True,
    )
    location = StockLocation.objects.create(
        name='Main stock', type='WAREHOUSE', branch_id='branch1',
    )
    rows = [
        ('A', '2', '10', '3'),
        ('B', '10', '20', '5'),
        ('C', '0', '30', '0'),
    ]
    for name, quantity, cost, reorder in rows:
        item = StockItem.objects.create(
            name=name, sku=f'RAW-{name}', base_unit=unit,
            item_type='RAW', avg_cost_price=cost,
            reorder_point=reorder, branch_id='branch1',
        )
        StockLevel.objects.create(
            stock_item=item, location=location, quantity=quantity,
            branch_id='branch1',
        )

    result, status = InventoryControlService.get(
        actor=actor, page=1, per_page=1,
    )

    assert status == 200, result
    assert len(result['data']['items']) == 1
    assert result['data']['summary'] == {
        'inventory_value_uzs': 220,
        'available_value_uzs': 220,
        'raw_item_count': 3,
        'low_stock_count': 2,
        'out_of_stock_count': 1,
        'supplier_payable_uzs': 0,
        'supplier_credit_uzs': 0,
        'valuation_method': 'WEIGHTED_AVERAGE',
        'as_of': result['data']['summary']['as_of'],
    }
    assert result['data']['items'][0]['stock_item']['name'] == 'C'


def test_inventory_missing_cost_returns_null_and_stable_issue():
    actor = _user('unsafe-inventory@test.local')
    item, _location = _raw_stock(cost='0')

    result, status = InventoryControlService.get(actor=actor)

    assert status == 200
    assert result['data']['summary']['inventory_value_uzs'] is None
    assert result['data']['items'][0]['inventory_value_uzs'] is None
    assert any(
        row['code'] == 'STOCK_COST_MISSING'
        and row['entity_id'] == item.id
        for row in result['data']['completeness']['issues']
    )


def test_inventory_integrity_issues_are_stable_and_preferred_supplier_is_deterministic():
    actor = _user('inventory-integrity@test.local')
    unit = StockUnit.objects.create(
        name='Piece', short_name='pc', unit_type='COUNT', is_base_unit=True,
    )
    location = StockLocation.objects.create(
        name='Integrity warehouse', type='WAREHOUSE', branch_id='branch1',
    )

    def item(name, quantity, *, reserved='0', tracked=False):
        row = StockItem.objects.create(
            name=name,
            sku=f'INTEGRITY-{name}',
            base_unit=unit,
            item_type='RAW',
            avg_cost_price='100',
            reorder_point='2',
            track_batches=tracked,
            branch_id='branch1',
        )
        StockLevel.objects.create(
            stock_item=row,
            location=location,
            quantity=quantity,
            reserved_quantity=reserved,
            branch_id='branch1',
        )
        return row

    negative = item('Negative', '-1')
    overflow = item('Reserved overflow', '5', reserved='6')
    mismatched = item('Batch mismatch', '10', tracked=True)
    StockBatch.objects.create(
        batch_number='MISMATCH-1',
        stock_item=mismatched,
        location=location,
        initial_quantity='8',
        current_quantity='8',
        unit_cost='100',
        total_cost='800',
        branch_id='branch1',
    )
    preferred_item = item('Preferred supplier', '10')
    suppliers = [
        Supplier.objects.create(name=name, branch_id='branch1')
        for name in ['Supplier B', 'Supplier A']
    ]
    links = [
        SupplierStockItem.objects.create(
            supplier=supplier,
            stock_item=preferred_item,
            unit=unit,
            price='100',
            is_preferred=True,
            branch_id='branch1',
        )
        for supplier in suppliers
    ]
    unsupported = Supplier.objects.create(
        name='Foreign supplier', currency='USD', current_balance='100',
        branch_id='branch1',
    )

    result, status = InventoryControlService.get(actor=actor, per_page=100)

    assert status == 200, result
    issues = result['data']['completeness']['issues']
    assert {
        (row['code'], row['entity_id']) for row in issues
    }.issuperset({
        ('STOCK_LEVEL_NEGATIVE', negative.id),
        ('STOCK_RESERVED_EXCEEDS_ON_HAND', overflow.id),
        ('STOCK_LEVEL_BATCH_MISMATCH', mismatched.id),
        ('DUPLICATE_PREFERRED_SUPPLIER', preferred_item.id),
        ('SUPPLIER_CURRENCY_UNSUPPORTED', unsupported.id),
    })
    assert result['data']['summary']['supplier_payable_uzs'] is None
    preferred_row = next(
        row for row in result['data']['items']
        if row['stock_item']['id'] == preferred_item.id
    )
    assert preferred_row['preferred_supplier']['supplier_id'] == links[0].supplier_id


def test_money_control_complete_snapshot_counts_paid_expense_once(monkeypatch):
    local_now = timezone.localtime(timezone.now())
    frozen = timezone.make_aware(
        datetime.combine(local_now.date(), time(12, 0)),
        timezone.get_current_timezone(),
    )
    monkeypatch.setattr(timezone, 'now', lambda: frozen)
    requester = _user('requester@test.local')
    approver = _user('approver@test.local')
    _credit_treasury('branch1', '1000', '2000', approver)
    _raw_stock(quantity='5', cost='100')
    category = ExpenseCategory.objects.create(
        code='UTILITIES', name='Utilities', allowed_sources=['BANK'],
    )
    created, status = ExpenseService.create(
        actor=requester,
        category_id=category.id,
        amount_uzs=100,
        requested_source='BANK',
        expense_date=timezone.localdate(),
        description='Internet',
    )
    assert status == 201, created
    expense_id = created['data']['expense_id']
    approved, status = ExpenseService.approve(expense_id, actor=approver)
    assert status == 200, approved
    paid, status = ExpenseService.pay(
        expense_id,
        actor=approver,
        source_account='BANK',
        fee_percent='10',
        action_id=uuid4(),
        idempotency_key='expense-1',
    )
    assert status == 200, paid

    result, status = MoneyControlService.overview(
        actor=approver,
        date_from=timezone.localdate(),
        date_to=timezone.localdate(),
    )

    assert status == 200, result
    data = result['data']
    assert data['completeness'] == {'status': 'COMPLETE', 'issues': []}
    assert data['treasury'] == {
        'safe_uzs': 1000,
        'bank_uzs': 1890,
        'drawer_unreconciled_uzs': 0,
        'liquid_total_uzs': 2890,
    }
    assert data['inventory']['raw_material_value_uzs'] == 500
    assert data['expenses']['paid_uzs'] == 110
    assert data['expenses']['by_category'] == [{
        'category_id': category.id,
        'category_name': 'Utilities',
        'paid_uzs': 110,
        'transaction_count': 1,
    }]
    assert data['working_capital'] == {
        'amount_uzs': 3390,
        'formula': WORKING_CAPITAL_FORMULA,
    }
    assert data['reconciliation'] == {'status': 'BALANCED', 'issues': []}
    assert Expense.objects.get(pk=expense_id).treasury_transaction_id == (
        paid['data']['treasury_transaction_id']
    )


def test_expense_lifecycle_fee_idempotency_and_void():
    requester = _user('expense-requester@test.local')
    approver = _user('expense-approver@test.local')
    _credit_treasury('branch1', '0', '1000000', approver)
    category = ExpenseCategory.objects.create(
        code='REPAIRS', name='Repairs', allowed_sources=['SAFE', 'BANK'],
    )
    created, status = ExpenseService.create(
        actor=requester,
        category_id=category.id,
        amount_uzs=350000,
        requested_source='BANK',
        expense_date=timezone.localdate(),
    )
    assert status == 201
    expense_id = created['data']['expense_id']
    denied, status = ExpenseService.approve(expense_id, actor=requester)
    assert status == 403
    assert denied['code'] == 'EXPENSE_SELF_APPROVAL_FORBIDDEN'
    ExpenseService.approve(expense_id, actor=approver)
    action_id = uuid4()
    paid, status = ExpenseService.pay(
        expense_id,
        actor=approver,
        source_account='BANK',
        fee_percent='1.5',
        action_id=action_id,
        idempotency_key='pay-one',
    )
    assert status == 200
    assert paid['data']['fee_uzs'] == 5250
    assert paid['data']['total_debited_uzs'] == 355250
    replay, status = ExpenseService.pay(
        expense_id,
        actor=approver,
        source_account='BANK',
        fee_percent='1.5',
        action_id=action_id,
        idempotency_key='pay-one',
    )
    assert status == 200
    assert replay['data'] == paid['data']
    conflict, status = ExpenseService.pay(
        expense_id,
        actor=approver,
        source_account='BANK',
        action_id=uuid4(),
    )
    assert status == 409
    assert conflict['code'] == 'EXPENSE_ALREADY_PAID'
    voided, status = ExpenseService.void(
        expense_id,
        actor=approver,
        reason='Invoice canceled',
        action_id=uuid4(),
        idempotency_key='void-one',
    )
    assert status == 200, voided
    assert voided['data']['expense']['status'] == 'VOIDED'
    assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('1000000')
    assert TreasuryTransaction.objects.count() == 4


def test_canonical_category_validation_and_cashbox_alias():
    actor = _user('category@test.local')
    invalid, status = ExpenseCategoryService.create(
        name='Invalid',
        is_active='false',
        actor=actor,
    )
    assert status == 422
    assert invalid['errors']['is_active'] == ['Use a JSON boolean.']
    unknown, status = ExpenseCategoryService.create(
        name='Unknown',
        actor=actor,
        unexpected=True,
    )
    assert status == 422
    assert unknown['errors']['unexpected'] == ['Unknown field.']
    created, status = ExpenseCategoryService.create(
        name='Drawer supplies',
        code='DRAWER_SUPPLIES',
        allowed_sources=['DRAWER'],
        actor=actor,
    )
    assert status == 201
    listed, status = CashboxCategoryService.list()
    assert status == 200
    assert listed['data'][0]['id'] == created['data']['category']['id']


def test_expense_creation_rejects_cross_branch_injection():
    actor = _user('branch-expense@test.local')
    category = ExpenseCategory.objects.create(
        code='BRANCH_TEST', name='Branch test', allowed_sources=['BANK'],
    )
    result, status = ExpenseService.create(
        actor=actor,
        branch_id='another-branch',
        category_id=category.id,
        amount_uzs=100,
        requested_source='BANK',
        expense_date=timezone.localdate(),
    )
    assert status == 403
    assert result['code'] == 'LOCATION_FORBIDDEN'
    assert not Expense.objects.exists()


def test_treasury_reconciliation_does_not_hide_wrong_branch_ledger_rows():
    safe = TreasuryAccount.objects.create(
        kind='SAFE', balance='100', branch_id='branch1',
    )
    TreasuryAccount.objects.create(
        kind='BANK', balance='0', branch_id='branch1',
    )
    transaction = TreasuryTransaction.objects.create(
        account=safe,
        type=TreasuryTransaction.Type.ADJUSTMENT,
        delta='100',
        balance_before='0',
        balance_after='100',
        branch_id='cloud',
    )

    treasury, issues = MoneyControlService._treasury('branch1')

    assert treasury == {'safe_uzs': None, 'bank_uzs': 0}
    mismatch = next(
        row for row in issues
        if row['code'] == 'TREASURY_LEDGER_BRANCH_MISMATCH'
    )
    assert mismatch['entity_id'] == transaction.id
    assert mismatch['details'] == {
        'account': 'SAFE',
        'account_branch_id': 'branch1',
        'transaction_branch_id': 'cloud',
    }
