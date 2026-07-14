import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_scope_epoch_repairs_children_quarantines_peer_rows_and_replays(settings):
    from base.models import (
        CashReconciliation,
        Category,
        Order,
        OrderItem,
        Product,
        Shift,
        SyncQueueRecord,
        SyncState,
        User,
    )
    from base.services.sync.status import SyncStatus

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'

    # Global same-company identity/catalog stays visible even though its owner
    # is the cloud rather than this terminal.
    cashier = User.objects.create(
        first_name='Shared', last_name='Cashier',
        email='epoch-cashier@example.test', password='!',
        role=User.RoleChoices.CASHIER, branch_id='cloud',
    )
    category = Category.objects.create(
        name='Shared catalog', slug='epoch-shared', branch_id='cloud',
    )
    product = Product.objects.create(
        category=category, name='Shared item', price='10000', branch_id='cloud',
    )

    own_order = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch-a',
        subtotal='10000', total_amount='10000',
    )
    own_item = OrderItem.objects.create(
        order=own_order, product=product, branch_id='branch-a',
        quantity=1, original_price='10000', price='10000',
    )
    peer_order = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch-b',
        subtotal='20000', total_amount='20000',
    )
    target_replay = peer_order.to_sync_dict()
    target_replay.update({
        'branch_id': 'branch-a',
        'is_deleted': False,
        'sync_version': peer_order.sync_version + 10,
        'description': 'authoritative target replay',
    })
    # Reproduce old-feed pollution where a child was stamped as local even
    # though its authoritative parent belongs to the peer branch.
    polluted_item = OrderItem.objects.create(
        order=peer_order, product=product, branch_id='branch-a',
        quantity=2, original_price='10000', price='10000',
    )
    blank_root = Order.objects.create(
        user=cashier, cashier=cashier, branch_id='branch-a',
        subtotal='30000', total_amount='30000',
    )
    Order._base_manager.filter(pk=blank_root.pk).update(branch_id='')
    blank_root.refresh_from_db()
    blank_child = OrderItem.objects.create(
        order=blank_root,
        product=product,
        branch_id='branch-a',
        quantity=3,
        original_price='10000',
        price='10000',
    )
    OrderItem._base_manager.filter(pk=blank_child.pk).update(branch_id='')
    blank_child.refresh_from_db()

    peer_shift = Shift.objects.create(
        user=cashier,
        start_time=timezone.now(),
        end_time=timezone.now(),
        status=Shift.Status.ENDED,
        branch_id='branch-b',
    )
    reconciliation = CashReconciliation.objects.create(
        shift=peer_shift,
        expected_cash='10',
        actual_cash='10',
        difference='0',
        reconciled_by=cashier,
        branch_id='branch-a',
    )

    # Preserve explicit queue evidence so the cleanup must remove both a peer
    # parent and the child that only becomes peer-owned after FK repair.
    SyncQueueRecord.objects.update_or_create(
        model_name='order', record_uuid=peer_order.uuid,
        defaults={'payload': peer_order.to_sync_dict()},
    )
    SyncQueueRecord.objects.update_or_create(
        model_name='orderitem', record_uuid=polluted_item.uuid,
        defaults={'payload': polluted_item.to_sync_dict()},
    )
    SyncState.objects.update_or_create(
        key=SyncStatus.CURSOR_KEY,
        defaults={'value': '2026-07-01T12:00:00+00:00'},
    )
    SyncState.objects.update_or_create(
        key=SyncStatus.SCOPE_EPOCH_KEY,
        defaults={'value': 'legacy-peer-exclusion'},
    )

    assert SyncStatus.ensure_scope_epoch() is True

    own_order.refresh_from_db()
    own_item.refresh_from_db()
    category.refresh_from_db()
    product.refresh_from_db()
    blank_root.refresh_from_db()
    blank_child.refresh_from_db()
    peer_order = Order._base_manager.get(pk=peer_order.pk)
    polluted_item = OrderItem._base_manager.get(pk=polluted_item.pk)
    peer_shift = Shift._base_manager.get(pk=peer_shift.pk)
    reconciliation = CashReconciliation._base_manager.get(pk=reconciliation.pk)

    assert own_order.is_deleted is False
    assert own_item.is_deleted is False
    assert category.is_deleted is False
    assert product.is_deleted is False
    assert blank_root.branch_id == 'branch-a'
    assert blank_root.is_deleted is False
    assert blank_child.branch_id == 'branch-a'
    assert blank_child.is_deleted is False
    assert peer_order.branch_id == 'branch-b'
    assert peer_order.is_deleted is True
    assert polluted_item.branch_id == 'branch-b'  # repaired from its parent
    assert polluted_item.is_deleted is True       # then quarantined, not erased
    # OneToOne ownership is part of the repair graph too.
    assert peer_shift.is_deleted is True
    assert reconciliation.branch_id == 'branch-b'
    assert reconciliation.is_deleted is True

    peer_marker = SyncStatus.scope_quarantine_key(Order, peer_order.uuid)
    item_marker = SyncStatus.scope_quarantine_key(OrderItem, polluted_item.uuid)
    reconciliation_marker = SyncStatus.scope_quarantine_key(
        CashReconciliation, reconciliation.uuid,
    )
    assert SyncState.objects.filter(key=peer_marker).exists()
    assert SyncState.objects.filter(key=item_marker).exists()
    assert SyncState.objects.filter(key=reconciliation_marker).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='order', record_uuid=peer_order.uuid,
    ).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='orderitem', record_uuid=polluted_item.uuid,
    ).exists()
    assert SyncState.objects.get(key=SyncStatus.CURSOR_KEY).value == ''
    assert SyncState.objects.get(
        key=SyncStatus.SCOPE_EPOCH_KEY,
    ).value == SyncStatus.SCOPE_EPOCH

    # A replay from the correctly targeted feed restores a mistakenly tagged
    # own row and retires its recovery marker in the same record transaction.
    from base.services.sync.service import SyncService
    applied = SyncService._apply_records(Order, [target_replay])
    assert applied['updated'] == 1
    peer_order.refresh_from_db()
    assert peer_order.branch_id == 'branch-a'
    assert peer_order.is_deleted is False
    assert peer_order.description == 'authoritative target replay'
    assert not SyncState.objects.filter(key=peer_marker).exists()

    # Idempotence: a restart/post_migrate retry must not touch data again.
    assert SyncStatus.ensure_scope_epoch() is False
