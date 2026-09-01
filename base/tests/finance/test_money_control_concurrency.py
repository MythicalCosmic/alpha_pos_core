from decimal import Decimal
from threading import Barrier, Lock, Thread
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from base.models import (
    CashReconciliation,
    Shift,
    TreasuryAccount,
    TreasuryTransaction,
    User,
)
from base.services.treasury_service import (
    TreasuryService,
    _apply,
    _lock_accounts,
)
from hr.models import ExpenseCategory
from hr.services.expense_service import ExpenseService
from stock.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    StockItem,
    StockLevel,
    StockLocation,
    StockSettings,
    StockTransaction,
    StockUnit,
    Supplier,
    SupplierPayment,
    SupplierTransaction,
)
from stock.services.purchase_service import PurchaseReceivingService
from stock.services.supplier_ledger_service import SupplierPaymentService


def _postgres_only():
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL row locks')


def _user(email):
    return User.objects.create(
        first_name=email.split('@')[0],
        last_name='Concurrent',
        email=email,
        password='!',
        role='ADMIN',
        status='ACTIVE',
        permissions=['*'],
        branch_id='branch1',
    )


def _parallel(operation):
    barrier = Barrier(2)
    lock = Lock()
    results = []
    failures = []

    def runner():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            value = operation()
            with lock:
                results.append(value)
        except Exception as exc:
            with lock:
                failures.append(exc)
        finally:
            close_old_connections()

    threads = [Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_shift_settlement_posts_each_tender_once():
    _postgres_only()
    actor = _user('settlement-concurrent@test.local')
    shift = Shift.objects.create(
        user=actor,
        start_time=timezone.now(),
        end_time=timezone.now(),
        status=Shift.Status.ENDED,
        treasury_settlement_eligible=True,
        branch_id='branch1',
    )
    CashReconciliation.objects.create(
        shift=shift,
        expected_cash='100',
        actual_cash='100',
        reconciled_by=actor,
        branch_id='branch1',
    )
    action_id = uuid4()

    def post():
        worker = User.objects.get(pk=actor.pk)
        return TreasuryService.post_shift_settlement(
            shift.id,
            {'CASH': 100, 'UZCARD': 200},
            performed_by=worker,
            branch_id='branch1',
            command_id=action_id,
            idempotency_key='settlement-concurrent',
        )

    results = _parallel(post)
    assert len(results) == 2
    assert results[0]['postings'] == results[1]['postings']
    assert TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
    ).count() == 2
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('100')
    assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('200')


@pytest.mark.django_db(transaction=True)
def test_concurrent_expense_payment_creates_one_debit():
    _postgres_only()
    requester = _user('expense-request@test.local')
    payer = _user('expense-pay@test.local')
    with transaction.atomic():
        bank = _lock_accounts(['BANK'], 'branch1')['BANK']
        _apply(
            bank, Decimal('1000'), TreasuryTransaction.Type.ADJUSTMENT,
            branch_id='branch1', performed_by=payer,
        )
    category = ExpenseCategory.objects.create(
        code='CONCURRENT_EXPENSE',
        name='Concurrent expense',
        allowed_sources=['BANK'],
    )
    created, _ = ExpenseService.create(
        actor=requester,
        category_id=category.id,
        amount_uzs=100,
        requested_source='BANK',
        expense_date=timezone.localdate(),
    )
    expense_id = created['data']['expense_id']
    ExpenseService.approve(expense_id, actor=payer)
    action_id = uuid4()

    def pay():
        worker = User.objects.get(pk=payer.pk)
        return ExpenseService.pay(
            expense_id,
            actor=worker,
            source_account='BANK',
            action_id=action_id,
            idempotency_key='expense-concurrent',
        )

    results = _parallel(pay)
    assert [status for _body, status in results] == [200, 200]
    transaction_ids = {
        body['data']['treasury_transaction_id'] for body, _status in results
    }
    assert len(transaction_ids) == 1
    assert TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.EXPENSE,
    ).count() == 1
    assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('900')


@pytest.mark.django_db(transaction=True)
def test_concurrent_receiving_and_supplier_payment_post_once():
    _postgres_only()
    actor = _user('supplier-concurrent@test.local')
    unit = StockUnit.objects.create(
        name='Piece', short_name='pc', unit_type='COUNT', is_base_unit=True,
    )
    location = StockLocation.objects.create(
        name='Warehouse', type='WAREHOUSE', branch_id='branch1',
    )
    item = StockItem.objects.create(
        name='Box', sku='CONCURRENT-BOX', base_unit=unit,
        item_type='RAW', branch_id='branch1',
    )
    supplier = Supplier.objects.create(name='Concurrent Supplier', branch_id='branch1')
    po = PurchaseOrder.objects.create(
        order_number='PO-CONCURRENT',
        supplier=supplier,
        delivery_location=location,
        status=PurchaseOrder.Status.CONFIRMED,
        order_date=timezone.localdate(),
        payment_due_date=timezone.now(),
        total='500',
        created_by=actor,
        branch_id='branch1',
    )
    line = PurchaseOrderItem.objects.create(
        purchase_order=po,
        stock_item=item,
        quantity_ordered='5',
        unit=unit,
        unit_price='100',
        total_price='500',
        branch_id='branch1',
    )
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.save(update_fields=['stock_enabled', 'updated_at'])
    created, _ = PurchaseReceivingService.create(
        po.id, actor.id, location.id,
    )
    receiving_id = created['data']['id']
    PurchaseReceivingService.add_item(
        receiving_id, line.id, '5', unit_cost='100',
    )
    receiving_action = uuid4()

    def complete():
        worker = User.objects.get(pk=actor.pk)
        return PurchaseReceivingService.complete(
            receiving_id,
            actor=worker,
            action_id=receiving_action,
            idempotency_key='receiving-concurrent',
        )

    completed = _parallel(complete)
    assert [status for _body, status in completed] == [200, 200]
    assert StockLevel.objects.get(stock_item=item).quantity == Decimal('5')
    assert StockTransaction.objects.filter(
        reference_type='PurchaseReceiving',
    ).count() == 1
    assert SupplierTransaction.objects.filter(
        type=SupplierTransaction.Type.PURCHASE,
    ).count() == 1

    with transaction.atomic():
        bank = _lock_accounts(['BANK'], 'branch1')['BANK']
        _apply(
            bank, Decimal('1000'), TreasuryTransaction.Type.ADJUSTMENT,
            branch_id='branch1', performed_by=actor,
        )
    payment_action = uuid4()

    def pay_supplier():
        worker = User.objects.get(pk=actor.pk)
        return SupplierPaymentService.pay(
            supplier.id,
            '500',
            'BANK',
            allocation_mode='EXPLICIT',
            allocations=[{'purchase_order_id': po.id, 'amount_uzs': 500}],
            actor=worker,
            action_id=payment_action,
            idempotency_key='supplier-concurrent',
        )

    payments = _parallel(pay_supplier)
    assert [status for _body, status in payments] == [201, 201]
    assert SupplierPayment.objects.count() == 1
    assert TreasuryTransaction.objects.filter(
        type=TreasuryTransaction.Type.SUPPLIER_PAYMENT,
    ).count() == 1
    supplier.refresh_from_db()
    assert supplier.current_balance == Decimal('0')
