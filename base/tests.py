"""Regression tests for base / sync bugs."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


class TestSyncConflictTiebreaker:
    """Higher sync_version always wins. On an equal-version conflict the policy
    is BRANCH-dominant: a branch (mode='local') keeps its own row, while the
    cloud (mode='cloud') accepts the incoming branch push. This is deterministic
    and doesn't depend on cross-machine clock skew."""

    def test_higher_version_wins(self):
        from base.models import User

        local = User.objects.create(
            first_name='Old', last_name='Name', email='u@test.local',
            password='hashed', role='USER', sync_version=2,
        )
        User.from_sync_dict({
            'uuid': str(local.uuid),
            'sync_version': 3,
            'is_deleted': False,
            'first_name': 'New',
            'last_name': 'Name',
            'email': 'u@test.local',
            'password': 'hashed',
            'role': 'USER',
        })
        local.refresh_from_db()
        assert local.first_name == 'New'

    def test_lower_version_does_not_overwrite(self):
        from base.models import User

        local = User.objects.create(
            first_name='Local', last_name='Name', email='u@test.local',
            password='hashed', role='USER', sync_version=5,
        )
        User.from_sync_dict({
            'uuid': str(local.uuid),
            'sync_version': 3,
            'is_deleted': False,
            'first_name': 'Old',
            'last_name': 'Name',
            'email': 'u@test.local',
            'password': 'hashed',
            'role': 'USER',
        })
        local.refresh_from_db()
        assert local.first_name == 'Local', 'older version must not overwrite'

    @override_settings(BRANCH_ID='branch1')
    def test_tie_branch_keeps_its_own_record(self):
        """On a branch, an equal-version conflict on the branch's OWN record
        (branch_id == ours) keeps local — the till's transactional data is never
        clobbered by a stale cloud echo. This is the 'local dominant' intent."""
        from base.models import User

        local = User.objects.create(
            first_name='Local', last_name='Name', email='u@test.local',
            password='hashed', role='USER', sync_version=3, branch_id='branch1',
        )
        User.from_sync_dict({
            'uuid': str(local.uuid), 'sync_version': 3, 'is_deleted': False,
            'first_name': 'Echo', 'last_name': 'Name', 'email': 'u@test.local',
            'password': 'hashed', 'role': 'USER', 'branch_id': 'branch1',
        })
        local.refresh_from_db()
        assert local.first_name == 'Local'

    @override_settings(BRANCH_ID='branch1')
    def test_tie_branch_accepts_cloud_owned_record(self):
        """On a branch, an equal-version conflict on a CLOUD-owned record
        (branch_id != ours) accepts the incoming change — otherwise an
        authoritative cloud edit (password reset, price change) would be silently
        rejected on a version tie. Regression for the local-dominant bug."""
        from base.models import User

        local = User.objects.create(
            first_name='Stale', last_name='Name', email='u@test.local',
            password='old', role='USER', sync_version=3, branch_id='cloud',
        )
        User.from_sync_dict({
            'uuid': str(local.uuid), 'sync_version': 3, 'is_deleted': False,
            'first_name': 'FromCloud', 'last_name': 'Name', 'email': 'u@test.local',
            'password': 'new-hash', 'role': 'USER', 'branch_id': 'cloud',
        })
        local.refresh_from_db()
        assert local.first_name == 'FromCloud'
        assert local.password == 'new-hash'

    @override_settings(DEPLOYMENT_MODE='cloud')
    def test_equal_version_cloud_accepts_branch_push(self):
        """On the cloud (mode='cloud'), an equal-version conflict accepts the
        incoming branch push — the branch owns its transactional data."""
        from base.models import User

        local = User.objects.create(
            first_name='Local', last_name='Name', email='u@test.local',
            password='hashed', role='USER', sync_version=3,
        )
        User.from_sync_dict({
            'uuid': str(local.uuid),
            'sync_version': 3,
            'is_deleted': False,
            'first_name': 'FromBranch',
            'last_name': 'Name',
            'email': 'u@test.local',
            'password': 'hashed',
            'role': 'USER',
        })
        local.refresh_from_db()
        assert local.first_name == 'FromBranch'


class TestUserCredentialSync:
    """Central user management: the owner creates/edits users on the cloud hub
    and they must work on every terminal. So sync propagates the password HASH
    (PBKDF2, portable across machines), role, permissions and status — single-
    tenant deployment, the cloud and branches are one operator's. (This
    deliberately reverses the earlier denylist; see User.SYNC_WRITE_DENYLIST.)"""

    def test_pull_propagates_credentials_and_role(self):
        from base.models import User

        local = User.objects.create(
            first_name='Local', last_name='Name', email='u@test.local',
            password='old-hash', role='USER', sync_version=2,
        )
        User.from_sync_dict({
            'uuid': str(local.uuid),
            'sync_version': 3,
            'is_deleted': False,
            'first_name': 'Local',
            'last_name': 'Name',
            'email': 'u@test.local',
            'password': 'new-hash',
            'role': 'ADMIN',
            'status': 'ACTIVE',
            'permissions': ['stock.view'],
        })
        local.refresh_from_db()
        assert local.role == 'ADMIN'
        assert local.password == 'new-hash'
        assert local.permissions == ['stock.view']

    def test_pull_reconciles_email_collision_instead_of_dropping(self):
        # A server-created user whose email matches an existing local row (e.g.
        # a bootstrap admin) must reconcile onto that row, not raise an
        # IntegrityError that silently drops it. The local row converges on the
        # incoming uuid.
        from base.models import User
        import uuid as uuid_module

        local = User.objects.create(
            first_name='Boot', last_name='Admin', email='admin@test.local',
            password='boot-hash', role='ADMIN', sync_version=1,
        )
        incoming_uuid = str(uuid_module.uuid4())
        instance, action = User.from_sync_dict({
            'uuid': incoming_uuid,
            'sync_version': 5,
            'is_deleted': False,
            'first_name': 'Server',
            'last_name': 'Admin',
            'email': 'admin@test.local',
            'password': 'server-hash',
            'role': 'ADMIN',
            'status': 'ACTIVE',
        })
        assert action == 'updated'
        assert User.objects.filter(email='admin@test.local').count() == 1
        reconciled = User.objects.get(email='admin@test.local')
        assert str(reconciled.uuid) == incoming_uuid
        assert reconciled.first_name == 'Server'
        assert reconciled.password == 'server-hash'

    def test_receive_ignores_spoofed_branch_id(self):
        from base.models import User
        from base.services.sync.receiver import CloudReceiver

        result = CloudReceiver.receive_batch(
            'base.User',
            branch_id='branch-a',
            records=[{
                'uuid': '11111111-1111-1111-1111-111111111111',
                'sync_version': 1,
                'is_deleted': False,
                # Attacker-controlled spoof attempt; receiver must ignore it.
                'branch_id': 'branch-b',
                'first_name': 'Spoof', 'last_name': 'Try',
                'email': 'spoof@test.local',
            }],
        )
        assert result['created'] == 1
        u = User.objects.get(uuid='11111111-1111-1111-1111-111111111111')
        assert u.branch_id == 'branch-a'


class TestDurableSyncQueue:
    """Pre-fix: queue lived only in cache; LocMem default lost it on
    process restart and Redis crashes between flushes lost unsent records.
    Now: SyncQueueRecord row per (model, uuid) survives process restart."""

    def test_add_persists_to_db(self):
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord
        import uuid as uuid_module

        u = uuid_module.uuid4()
        SyncQueue.add('user', str(u), {'uuid': str(u), 'first_name': 'Persisted'})

        assert SyncQueueRecord.objects.filter(record_uuid=u).exists()

    def test_add_is_upsert_on_model_uuid(self):
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord
        import uuid as uuid_module

        u = uuid_module.uuid4()
        SyncQueue.add('user', str(u), {'first_name': 'V1'})
        SyncQueue.add('user', str(u), {'first_name': 'V2'})

        rows = SyncQueueRecord.objects.filter(record_uuid=u)
        assert rows.count() == 1
        assert rows.first().payload['first_name'] == 'V2'

    def test_remove_deletes_rows(self):
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord
        import uuid as uuid_module

        u1, u2 = uuid_module.uuid4(), uuid_module.uuid4()
        SyncQueue.add('user', str(u1), {'first_name': 'A'})
        SyncQueue.add('user', str(u2), {'first_name': 'B'})

        SyncQueue.remove([str(u1)])
        assert not SyncQueueRecord.objects.filter(record_uuid=u1).exists()
        assert SyncQueueRecord.objects.filter(record_uuid=u2).exists()

    def test_count_distinguishes_failed(self):
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord
        import uuid as uuid_module

        u1, u2 = uuid_module.uuid4(), uuid_module.uuid4()
        SyncQueue.add('user', str(u1), {'first_name': 'A'})
        SyncQueue.add('user', str(u2), {'first_name': 'B'})
        SyncQueue.mark_failed(str(u1), 'transport error')

        total, failed = SyncQueue.count()
        assert total == 2
        assert failed == 1


class TestInkassaAtomic:
    """InkassaService.add_to_register must use F() so two concurrent calls
    don't lose updates. Verify F-expression behavior with sequential adds."""

    def test_sequential_increments_accumulate(self):
        from base.models import CashRegister
        from base.services.inkassa_service import InkassaService

        CashRegister.objects.create(current_balance=Decimal('0'))
        InkassaService.add_to_register(Decimal('100'))
        InkassaService.add_to_register(Decimal('50'))
        InkassaService.add_to_register(Decimal('25'))

        register = CashRegister.objects.first()
        assert register.current_balance == Decimal('175')


class TestPartialUniqueAllowsEmpty:
    """Regression: partial unique constraints must NOT constrain empty values —
    many categories have no slug. This broke cloud sync with
    'duplicate key ... uniq_category_slug_active ... slug=()'."""

    def test_multiple_empty_slug_categories_ok(self):
        from base.models import Category
        Category.objects.create(name='A', slug='')
        Category.objects.create(name='B', slug='')
        assert Category.objects.filter(slug='').count() == 2

    def test_nonempty_slug_still_unique(self):
        from base.models import Category
        from django.db import IntegrityError, transaction
        Category.objects.create(name='A', slug='drinks')
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Category.objects.create(name='B', slug='drinks')


class TestCategorySlugReconcile:
    """Sync must reconcile an incoming category onto an existing same-slug row
    instead of INSERTing a duplicate (UNIQUE constraint failed: base_category.slug)."""

    def test_slug_collision_reconciles_not_inserts(self):
        from base.models import Category
        import uuid as _uuid
        Category.objects.create(name='Drinks', slug='drinks', sync_version=1)
        incoming = str(_uuid.uuid4())
        inst, action = Category.from_sync_dict({
            'uuid': incoming, 'sync_version': 5, 'is_deleted': False,
            'name': 'Beverages', 'slug': 'drinks',
        })
        assert action == 'updated'
        assert Category.objects.filter(slug='drinks').count() == 1
        assert str(Category.objects.get(slug='drinks').uuid) == incoming

    def test_empty_slug_incoming_inserts(self):
        from base.models import Category
        import uuid as _uuid
        Category.objects.create(name='A', slug='', sync_version=1)
        inst, action = Category.from_sync_dict({
            'uuid': str(_uuid.uuid4()), 'sync_version': 1, 'is_deleted': False,
            'name': 'B', 'slug': '',
        })
        assert action == 'created'
        assert Category.objects.filter(slug='').count() == 2


class TestRehashPasswords:
    """Repair for users created via Django /admin/ with a plaintext PIN."""

    def test_plaintext_pin_rehashed_and_verifies(self):
        from base.models import User
        from base.security.hashing import verify_password
        from django.core.management import call_command
        u = User.objects.create(
            first_name='P', last_name='IN', email='pin@t.local',
            password='2233', role='CASHIER', status='ACTIVE')
        # Plaintext can't be verified by check_password — this is the 401 cause.
        assert verify_password('2233', u.password) is False
        call_command('rehash_passwords', verbosity=0)
        u.refresh_from_db()
        assert verify_password('2233', u.password) is True

    def test_existing_hash_left_untouched(self):
        from base.models import User
        from base.security.hashing import hash_password, verify_password
        from django.core.management import call_command
        h = hash_password('9999')
        u = User.objects.create(
            first_name='H', last_name='Ash', email='hash@t.local',
            password=h, role='CASHIER', status='ACTIVE')
        call_command('rehash_passwords', verbosity=0)
        u.refresh_from_db()
        assert u.password == h  # untouched
        assert verify_password('9999', u.password) is True


class TestReceiveOwnershipPreserved:
    """A branch editing a CLOUD-owned record must not steal ownership on push —
    else /changes later excludes it from that branch and cloud edits stop
    flowing down (the 'edit on local renames it branch1' bug)."""

    def test_branch_edit_keeps_cloud_owner(self):
        from base.models import User
        from base.services.sync.receiver import CloudReceiver
        u = User.objects.create(
            first_name='Cloud', last_name='User', email='c@test.local',
            password='h', role='CASHIER', sync_version=1, branch_id='cloud')
        result = CloudReceiver.receive_batch('base.User', branch_id='branch1', records=[{
            'uuid': str(u.uuid), 'sync_version': 2, 'is_deleted': False,
            'first_name': 'Edited', 'last_name': 'User', 'email': 'c@test.local',
            'branch_id': 'cloud',
        }])
        assert result['updated'] == 1
        u.refresh_from_db()
        assert u.first_name == 'Edited'       # the edit applied
        assert u.branch_id == 'cloud'         # owner preserved (NOT branch1)

    def test_untagged_row_gets_tagged_on_update(self):
        from base.models import User
        from base.services.sync.receiver import CloudReceiver
        u = User.objects.create(
            first_name='No', last_name='Owner', email='n@test.local',
            password='h', role='CASHIER', sync_version=1)
        # save() auto-tags an empty branch_id with settings.BRANCH_ID, so force
        # it empty at the DB level to exercise "untagged -> tag with pusher".
        User.objects.filter(pk=u.pk).update(branch_id='')
        CloudReceiver.receive_batch('base.User', branch_id='branch1', records=[{
            'uuid': str(u.uuid), 'sync_version': 2, 'is_deleted': False,
            'first_name': 'No', 'last_name': 'Owner', 'email': 'n@test.local',
        }])
        u.refresh_from_db()
        assert u.branch_id == 'branch1'        # empty -> tagged with pusher


class TestReceiveResolvesNonBaseModels:
    """Bare model names from the push queue must resolve to the RIGHT app, not
    default to 'base' (which rejected every cashbox/stock/hr/discounts record:
    "App 'base' doesn't have a 'shiftpaymenttotal' model")."""

    def test_bare_cashbox_model_resolves(self):
        from base.services.sync.receiver import CloudReceiver
        result = CloudReceiver.receive_batch('shiftpaymenttotal', 'branch1', [])
        assert result['success'] is True

    def test_bare_stock_model_resolves(self):
        from base.services.sync.receiver import CloudReceiver
        result = CloudReceiver.receive_batch('stocklevel', 'branch1', [])
        assert result['success'] is True

    def test_bare_base_model_still_resolves(self):
        from base.services.sync.receiver import CloudReceiver
        result = CloudReceiver.receive_batch('user', 'branch1', [])
        assert result['success'] is True


class TestUnpaidExcludesCancelled:
    """Pre-fix: build_filtered_queryset(payment_status='UNPAID') only filtered
    is_paid=False, so a cancelled-but-unpaid order lingered forever in the
    cashier's unpaid list."""

    def _order(self, user, status, is_paid, display_id):
        from base.models import Order
        return Order.objects.create(
            user=user, status=status, is_paid=is_paid, display_id=display_id,
            subtotal='10.00', total_amount='10.00',
        )

    def test_unpaid_filter_excludes_cancelled(self, regular_user):
        from base.models import Order
        from base.repositories.order import OrderRepository
        live = self._order(regular_user, 'PREPARING', False, 1)
        self._order(regular_user, 'CANCELED', False, 2)  # the leaking row
        qs = OrderRepository.build_filtered_queryset(payment_status='UNPAID')
        ids = list(qs.values_list('id', flat=True))
        assert ids == [live.id]
        assert Order.Status.CANCELED not in set(qs.values_list('status', flat=True))

    def test_paid_filter_also_excludes_cancelled(self, regular_user):
        from base.repositories.order import OrderRepository
        paid = self._order(regular_user, 'COMPLETED', True, 1)
        self._order(regular_user, 'CANCELED', True, 2)  # cancelled-after-payment
        qs = OrderRepository.build_filtered_queryset(payment_status='PAID')
        assert list(qs.values_list('id', flat=True)) == [paid.id]


class TestChefQueueNumber:
    """The chef display number must keep increasing (never wrap at 100 like the
    cashier display_id) and must stay branch-local (never sync)."""

    def test_monotonic_past_100(self):
        from base.repositories.order import OrderRepository
        vals = [OrderRepository.next_chef_queue_number(scope='t') for _ in range(105)]
        assert vals == list(range(1, 106))  # 100 -> 101, never resets to 1

    def test_excluded_from_sync_dict(self, regular_user):
        from base.models import Order
        o = Order.objects.create(
            user=regular_user, status='PREPARING', is_paid=False,
            display_id=5, chef_queue_number=42, subtotal='10.00', total_amount='10.00',
        )
        data = o.to_sync_dict()
        assert 'chef_queue_number' not in data
        assert 'display_id' not in data


class TestPopularProductsFilter:
    """popular=True (default) orders products by recent sales; popular=False
    falls back to the requested ordering."""

    def test_popular_puts_top_seller_first(self, regular_user, category):
        from base.models import Product, Order, OrderItem
        from customers.services.product_service import CustomerProductService
        hot = Product.objects.create(name='Hot', price='10.00', category=category)
        cold = Product.objects.create(name='Cold', price='10.00', category=category)
        order = Order.objects.create(
            user=regular_user, status='COMPLETED', is_paid=True, display_id=1,
            subtotal='10.00', total_amount='10.00')
        OrderItem.objects.create(order=order, product=hot, quantity=50, price='10.00')

        res, st = CustomerProductService.get_all_products(popular=True)
        assert st == 200
        ids = [p['id'] for p in res['data']['products']]
        assert ids[0] == hot.id  # best seller floats to the top

        # popular=False uses the default -created_at: cold was created last.
        res2, _ = CustomerProductService.get_all_products(popular=False)
        ids2 = [p['id'] for p in res2['data']['products']]
        assert ids2[0] == cold.id

    def test_popular_respects_category(self, regular_user, category):
        from base.models import Category, Product, Order, OrderItem
        from customers.services.product_service import CustomerProductService
        other = Category.objects.create(name='Other')
        in_cat = Product.objects.create(name='InCat', price='10.00', category=category)
        Product.objects.create(name='Elsewhere', price='10.00', category=other)
        order = Order.objects.create(
            user=regular_user, status='COMPLETED', is_paid=True, display_id=1,
            subtotal='10.00', total_amount='10.00')
        OrderItem.objects.create(order=order, product=in_cat, quantity=5, price='10.00')

        res, st = CustomerProductService.get_all_products(
            popular=True, category_ids=[category.id])
        assert st == 200
        names = {p['name'] for p in res['data']['products']}
        assert names == {'InCat'}  # category filter still honoured under popular


class TestNotificationRouting:
    """Per-chat, per-category routing: each chat picks which message categories
    it receives. Sync messages ride the 'system' category."""

    def _settings(self, chat_ids, routing):
        from notifications.models import NotificationSettings
        ns = NotificationSettings.load()
        ns.chat_ids = chat_ids
        ns.chat_routing = routing
        ns.save()
        return ns

    def test_recipients_for_respects_routing(self):
        from notifications.models import NotificationSettings
        # 111 only wants order_paid; 222 isn't in the map ⇒ receives everything.
        self._settings(['111', '222'],
                       {'111': {'events': {'order_paid': True, 'daily': False, 'system': False}}})
        ns = NotificationSettings.load()
        assert ns.recipients_for('order_paid') == ['111', '222']
        assert ns.recipients_for('daily') == ['222']
        assert ns.recipients_for('system') == ['222']

    def test_sync_excludes_system_muted(self):
        from base.services.sync.service import SyncService
        # 222 muted from 'system' (the category sync messages ride on).
        self._settings(['111', '222', '333'], {'222': {'events': {'system': False}}})
        assert SyncService._sync_recipients() == ['111', '333']

    def test_default_all_receive(self):
        from base.services.sync.service import SyncService
        self._settings(['111', '222'], {})
        assert SyncService._sync_recipients() == ['111', '222']

    def test_bucket_for_maps_real_types(self):
        from notifications.models import bucket_for
        assert bucket_for('order_paid') == 'order_paid'
        assert bucket_for('hr.contract_expiry') == 'contract'
        assert bucket_for('hr.document_expiry') == 'document'
        assert bucket_for('daily_summary') == 'daily'
        assert bucket_for('telegram.start') == 'system'
