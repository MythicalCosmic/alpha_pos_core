import django
import pytest

django.setup()


@pytest.fixture(autouse=True)
def _single_branch_cloud_target(settings):
    """Match the explicit single-branch target used by production tests."""
    if not getattr(settings, 'CLOUD_DEFAULT_TARGET_BRANCH_ID', ''):
        settings.CLOUD_DEFAULT_TARGET_BRANCH_ID = 'branch1'


@pytest.fixture(autouse=True)
def _clear_caches():
    """LocMemCache is process-wide and survives across tests; explicitly
    purge it so cached settings singletons (NotificationSettings,
    LoyaltySettings, AppSettings, session rows) don't leak between cases."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _active_license(db, _clear_caches):
    """Every test should run against an ACTIVE license — the kill switch
    middleware otherwise refuses every business endpoint with 503 (since
    a freshly-migrated test DB has no License row, .load() creates one
    with status=UNREGISTERED).

    Tests that need to exercise the kill switch explicitly reset the
    License row to the state they want before the request (see
    licensing/tests/test_licensing.py TestKillSwitch / TestStateTransitions)."""
    from datetime import timedelta
    from django.utils import timezone
    from licensing.models import License
    lic = License.load()
    lic.status = License.Status.ACTIVE
    lic.org_name = 'Test Org'
    lic.email = 'test@local'
    lic.last_heartbeat_at = timezone.now()
    lic.last_server_now = timezone.now()
    lic.expires_at = timezone.now() + timedelta(days=365)
    lic.save()


@pytest.fixture
def admin_user(db):
    from base.models import User
    from base.security.hashing import hash_password
    return User.objects.create(
        first_name='Admin',
        last_name='Tester',
        email='admin@test.local',
        password=hash_password('adminpass'),
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def cashier_user(db):
    from base.models import User
    from base.security.hashing import hash_password
    return User.objects.create(
        first_name='Cashier',
        last_name='One',
        email='cashier1@test.local',
        password=hash_password('cashierpass'),
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def other_cashier_user(db):
    from base.models import User
    from base.security.hashing import hash_password
    return User.objects.create(
        first_name='Cashier',
        last_name='Two',
        email='cashier2@test.local',
        password=hash_password('cashierpass'),
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def regular_user(db):
    from base.models import User
    from base.security.hashing import hash_password
    return User.objects.create(
        first_name='User',
        last_name='One',
        email='user1@test.local',
        password=hash_password('userpass'),
        role=User.RoleChoices.USER,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def other_user(db):
    from base.models import User
    from base.security.hashing import hash_password
    return User.objects.create(
        first_name='User',
        last_name='Two',
        email='user2@test.local',
        password=hash_password('userpass'),
        role=User.RoleChoices.USER,
        status=User.UserStatus.ACTIVE,
    )


@pytest.fixture
def category(db):
    from base.models import Category
    return Category.objects.create(name='Test Category')


@pytest.fixture
def product(db, category):
    from base.models import Product
    return Product.objects.create(
        name='Test Product', price='10.00', category=category,
    )


@pytest.fixture
def order_factory(db, regular_user, product):
    from base.models import Order, OrderItem

    def _make(user=None, cashier=None, status='PREPARING', is_paid=False, items=1):
        order = Order.objects.create(
            user=user or regular_user,
            cashier=cashier,
            order_type='HALL',
            status=status,
            is_paid=is_paid,
            display_id=Order.objects.count() + 1,
            subtotal='10.00',
            total_amount='10.00',
        )
        for _ in range(items):
            OrderItem.objects.create(
                order=order, product=product, quantity=1, price=product.price,
            )
        return order

    return _make
