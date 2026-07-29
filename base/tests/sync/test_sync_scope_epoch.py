import json

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
        key=SyncStatus.cursor_key(),
        defaults={'value': '2026-07-01T12:00:00+00:00'},
    )
    SyncState.objects.update_or_create(
        key=SyncStatus.scope_epoch_key(),
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
    assert SyncState.objects.get(key=SyncStatus.cursor_key()).value == ''
    assert SyncState.objects.get(
        key=SyncStatus.scope_epoch_key(),
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


def test_scope_epoch_switch_a_b_a_resets_target_cursor_and_replays(settings):
    from base.models import Order, SyncQueueRecord, SyncState, User
    from base.services.sync.queue import SyncQueue
    from base.services.sync.service import SyncService
    from base.services.sync.status import SyncStatus

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = False
    settings.BRANCH_ID = 'branch-a'
    cashier = User.objects.create(
        first_name='Shared',
        last_name='Cashier',
        email='scope-switch@example.test',
        password='!',
        role=User.RoleChoices.CASHIER,
        branch_id='cloud',
    )
    order_a = Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id='branch-a',
        subtotal='10.00',
        total_amount='10.00',
    )
    order_b = Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id='branch-b',
        subtotal='20.00',
        total_amount='20.00',
    )
    replay_a = order_a.to_sync_dict()
    replay_a.update({
        'sync_version': order_a.sync_version + 10,
        'branch_id': 'branch-a',
        'is_deleted': False,
        'description': 'restored branch A',
    })
    replay_b = order_b.to_sync_dict()
    replay_b.update({
        'sync_version': order_b.sync_version + 10,
        'branch_id': 'branch-b',
        'is_deleted': False,
        'description': 'restored branch B',
    })

    # First activation establishes A and quarantines the foreign B row.
    assert SyncStatus.ensure_scope_epoch() is True
    assert SyncState.objects.get(
        key=SyncStatus.ACTIVE_SCOPE_BRANCH_KEY,
    ).value == 'branch-a'
    order_a.refresh_from_db()
    order_b.refresh_from_db()
    assert order_a.is_deleted is False
    assert order_b.is_deleted is True
    SyncStatus.set_cursor('2026-07-21T10:00:00+00:00')

    # Switch to B. Even though B has its own epoch key, the transition must
    # quarantine A and clear B's target cursor before the full feed replay.
    settings.BRANCH_ID = 'branch-b'
    SyncStatus.set_cursor('2026-07-22T10:00:00+00:00')
    assert SyncStatus.ensure_scope_epoch() is True
    assert SyncStatus.get_cursor() is None
    order_a.refresh_from_db()
    assert order_a.is_deleted is True
    restored_b = SyncService._apply_records(Order, [replay_b])
    assert restored_b['updated'] == 1
    order_b.refresh_from_db()
    assert order_b.is_deleted is False
    assert order_b.description == 'restored branch B'
    SyncStatus.set_cursor('2026-07-23T10:00:00+00:00')
    SyncQueue.add('order', order_b.uuid, order_b.to_sync_dict())

    # Returning to an already-migrated A must not early-return. It clears A's
    # old frontier, quarantines B (including its outbound slot), then a complete
    # A feed replay restores unchanged A history from its durable marker.
    settings.BRANCH_ID = 'branch-a'
    assert SyncStatus.get_cursor() == '2026-07-21T10:00:00+00:00'
    assert SyncStatus.ensure_scope_epoch() is True
    assert SyncStatus.get_cursor() is None
    assert SyncState.objects.get(
        key=SyncStatus.ACTIVE_SCOPE_BRANCH_KEY,
    ).value == 'branch-a'
    order_a.refresh_from_db()
    order_b.refresh_from_db()
    assert order_a.is_deleted is True
    assert order_b.is_deleted is True
    assert not SyncQueueRecord.objects.filter(
        model_name='order',
        record_uuid=order_b.uuid,
    ).exists()

    marker_b = SyncStatus.scope_quarantine_key(Order, order_b.uuid)
    marker_state = json.loads(SyncState.objects.get(key=marker_b).value)
    assert marker_state['original_branch_id'] == 'branch-b'
    assert marker_state['local_branch_id'] == 'branch-a'

    restored_a = SyncService._apply_records(Order, [replay_a])
    assert restored_a['updated'] == 1
    order_a.refresh_from_db()
    order_b.refresh_from_db()
    assert order_a.is_deleted is False
    assert order_a.description == 'restored branch A'
    assert order_b.is_deleted is True
    assert not SyncState.objects.filter(
        key=SyncStatus.scope_quarantine_key(Order, order_a.uuid),
    ).exists()
    assert SyncState.objects.filter(key=marker_b).exists()
    assert SyncStatus.ensure_scope_epoch() is False
