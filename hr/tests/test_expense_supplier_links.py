from datetime import date
from decimal import Decimal

import pytest

from base.financial import FinancialReportingGroup
from base.models import User
from hr.models import Expense, ExpenseCategory, ExpenseSupplierLink
from hr.services.expense_service import ExpenseService
from hr.services.expense_supplier_service import ExpenseSupplierService
from stock.models import Supplier


pytestmark = pytest.mark.django_db
BRANCH = 'branch1'


def _admin(email='links-admin@test.local', branch=BRANCH):
    return User.objects.create(
        first_name='Links', last_name='Admin', email=email, password='!',
        role=User.RoleChoices.ADMIN, status=User.UserStatus.ACTIVE,
        permissions=['*'], branch_id=branch,
    )


def _expense(category, amount, *, status=Expense.Status.PAID, source=Expense.Source.SAFE, day=date(2026, 8, 5)):
    return Expense.objects.create(
        category=category,
        category_code_snapshot=category.code,
        category_name_snapshot=category.name,
        category_reporting_group_snapshot=category.reporting_group,
        amount=Decimal(amount),
        expense_date=day,
        status=status,
        requested_source=source,
        description=f'{category.name} {amount}',
        branch_id=BRANCH,
    )


@pytest.fixture
def books():
    meat = ExpenseCategory.objects.create(
        code='MEAT', name='Meat', reporting_group=FinancialReportingGroup.INVENTORY_PURCHASE,
    )
    rent = ExpenseCategory.objects.create(code='RENT', name='Rent', reporting_group=FinancialReportingGroup.RENT)
    donar = Supplier.objects.create(name="Donar go'sht", branch_id=BRANCH)
    chicken = Supplier.objects.create(name='Marhamat chicken', branch_id=BRANCH)
    return {
        'admin': _admin(),
        'donar': donar,
        'chicken': chicken,
        'paid': _expense(meat, '4000000'),
        'till': _expense(meat, '500000', source=Expense.Source.DRAWER, day=date(2026, 8, 20)),
        'canceled': _expense(meat, '900000', status=Expense.Status.CANCELED),
        'rent': _expense(rent, '15000000'),
    }


def test_link_attributes_purchases_without_touching_money(books):
    paid, till = books['paid'], books['till']
    before = [(e.status, e.amount, e.requested_source) for e in (paid, till)]

    preview, status = ExpenseSupplierService.link(
        expense_ids=[paid.id, till.id], supplier_id=books['donar'].id, actor=books['admin'], dry_run=True,
    )
    assert status == 200
    assert preview['data']['link']['amount_uzs'] == 4_500_000
    assert not ExpenseSupplierLink.objects.exists()

    result, status = ExpenseSupplierService.link(
        expense_ids=[paid.id, till.id], supplier_id=books['donar'].id, actor=books['admin'], note='August books',
    )
    assert status == 200
    assert result['data']['link']['newly_linked'] == 2
    for expense in (paid, till):
        expense.refresh_from_db()
    assert [(e.status, e.amount, e.requested_source) for e in (paid, till)] == before

    purchases, status = ExpenseSupplierService.purchases(books['donar'].id, actor=books['admin'])
    assert status == 200
    assert purchases['data']['totals']['amount_uzs'] == 4_500_000
    assert purchases['data']['totals']['by_source'] == {
        'SAFE': {'count': 1, 'amount_uzs': 4_000_000},
        'DRAWER': {'count': 1, 'amount_uzs': 500_000},
    }
    assert [row['expense_id'] for row in purchases['data']['purchases']] == [till.id, paid.id]

    in_window, _ = ExpenseSupplierService.purchases(
        books['donar'].id, actor=books['admin'], date_from=date(2026, 8, 10),
    )
    assert in_window['data']['totals']['count'] == 1


def test_relinking_moves_a_purchase_to_another_supplier(books):
    paid = books['paid']
    ExpenseSupplierService.link(expense_ids=[paid.id], supplier_id=books['donar'].id, actor=books['admin'])

    result, status = ExpenseSupplierService.link(
        expense_ids=[paid.id], supplier_id=books['chicken'].id, actor=books['admin'],
    )

    assert status == 200
    assert result['data']['link']['moved_from_other_supplier'] == [paid.id]
    assert ExpenseSupplierLink.objects.get().supplier_id == books['chicken'].id

    removed, status = ExpenseSupplierService.unlink(expense_ids=[paid.id], actor=books['admin'])
    assert status == 200
    assert removed['data']['unlinked'] == 1


def test_only_active_supplier_purchases_can_be_linked(books):
    for expense in (books['rent'], books['canceled']):
        result, status = ExpenseSupplierService.link(
            expense_ids=[expense.id], supplier_id=books['donar'].id, actor=books['admin'],
        )
        assert status == 422
        assert result['code'] == 'EXPENSE_NOT_A_SUPPLIER_PURCHASE'

    other_branch = Supplier.objects.create(name='Elsewhere', branch_id='branch2')
    result, status = ExpenseSupplierService.link(
        expense_ids=[books['paid'].id], supplier_id=other_branch.id, actor=books['admin'],
    )
    assert status == 404

    for bad in ([], [0], [books['paid'].id, books['paid'].id], ['1'], [True]):
        _, status = ExpenseSupplierService.link(expense_ids=bad, supplier_id=books['donar'].id, actor=books['admin'])
        assert status == 422
    assert not ExpenseSupplierLink.objects.exists()


def test_expense_list_can_leave_out_or_show_only_supplier_purchases(books):
    ExpenseSupplierService.link(
        expense_ids=[books['paid'].id], supplier_id=books['donar'].id, actor=books['admin'],
    )

    def listed(**filters):
        body, status = ExpenseService.list(per_page=50, actor=books['admin'], view_all=True, **filters)
        assert status == 200
        return body['data']

    everything = listed()
    assert len(everything['expenses']) == 4
    linked_row = next(row for row in everything['expenses'] if row['id'] == books['paid'].id)
    assert linked_row['supplier'] == {'id': books['donar'].id, 'name': "Donar go'sht"}

    without = listed(supplier_purchases='exclude')
    assert books['paid'].id not in {row['id'] for row in without['expenses']}
    assert without['totals']['amount_uzs'] == 16_400_000

    only = listed(supplier_purchases='only')
    assert [row['id'] for row in only['expenses']] == [books['paid'].id]
    assert [row['id'] for row in listed(supplier_id=books['donar'].id)['expenses']] == [books['paid'].id]

    _, status = ExpenseService.list(actor=books['admin'], view_all=True, supplier_purchases='sometimes')
    assert status == 422
