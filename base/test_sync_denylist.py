"""SYNC_DENY_FROM_BRANCH must block a branch token from soft-deleting (or
escalating) cloud users via a sync push — is_deleted was previously applied
directly, bypassing the denylist."""
import pytest

pytestmark = pytest.mark.django_db


class TestBranchCannotSoftDeleteCloudUser:
    def test_branch_is_deleted_ignored_for_user_on_cloud(self, settings):
        settings.DEPLOYMENT_MODE = 'cloud'   # cloud-receive => SYNC_DENY_FROM_BRANCH applies
        from base.models import User
        from base.services.sync.receiver import CloudReceiver
        u = User.objects.create(first_name='Admin', last_name='X', email='admin@x.local',
                                role='ADMIN', status='ACTIVE', password='!')
        # A holder of a branch token pushes a higher-version record that tries to
        # soft-delete + downgrade the cloud admin.
        CloudReceiver._create_or_update(User, {
            'uuid': str(u.uuid), 'sync_version': 999, 'is_deleted': True,
            'first_name': 'Admin', 'last_name': 'X', 'email': 'admin@x.local',
            'role': 'CASHIER', 'status': 'SUSPENDED',
        }, 'branch1')
        u.refresh_from_db()
        assert u.is_deleted is False                 # branch CANNOT delete a cloud user
        assert u.role == 'ADMIN'                      # nor downgrade the role
        assert u.status == 'ACTIVE'                   # nor suspend

    def test_branch_cannot_mutate_any_user_identity_field(self, settings):
        settings.DEPLOYMENT_MODE = 'cloud'
        from base.models import User
        from base.services.sync.receiver import CloudReceiver

        user = User.objects.create(
            first_name='Cloud', last_name='Owner', email='owner@example.test',
            role='ADMIN', status='ACTIVE', password='cloud-secret',
            permissions=['reports'], branch_id='cloud',
        )
        result = CloudReceiver.receive_batch('user', 'branch-a', [{
            'uuid': str(user.uuid),
            'sync_version': user.sync_version + 500,
            'is_deleted': True,
            'first_name': 'Forged',
            'last_name': 'Identity',
            'email': 'attacker@example.test',
            'password': 'forged-secret',
            'role': 'CASHIER',
            'status': 'SUSPENDED',
            'permissions': [],
        }])

        assert result['skipped'] == 1
        assert result['updated'] == 0
        user.refresh_from_db()
        assert user.first_name == 'Cloud'
        assert user.last_name == 'Owner'
        assert user.email == 'owner@example.test'
        assert user.password == 'cloud-secret'
        assert user.role == 'ADMIN'
        assert user.status == 'ACTIVE'
        assert user.permissions == ['reports']
        assert user.is_deleted is False
        assert user.sync_version < 500

    def test_branch_cannot_create_cloud_owned_user(self, settings):
        settings.DEPLOYMENT_MODE = 'cloud'
        import uuid
        from base.models import User
        from base.services.sync.receiver import CloudReceiver

        result = CloudReceiver.receive_batch('user', 'branch-a', [{
            'uuid': str(uuid.uuid4()),
            'sync_version': 1,
            'is_deleted': False,
            'first_name': 'Forged',
            'last_name': 'Admin',
            'email': 'forged-admin@example.test',
            'password': 'secret',
            'role': 'ADMIN',
            'status': 'ACTIVE',
        }])

        assert result['skipped'] == 1
        assert not User._base_manager.filter(email='forged-admin@example.test').exists()


class TestBranchCannotMutateCloudCatalog:
    def test_product_scalars_fk_and_delete_are_all_ignored(self, settings):
        settings.DEPLOYMENT_MODE = 'cloud'
        from base.models import Category, Product
        from base.services.sync.receiver import CloudReceiver

        food = Category.objects.create(name='Food', slug='food', branch_id='cloud')
        forged_category = Category.objects.create(
            name='Forged', slug='forged', branch_id='cloud',
        )
        product = Product.objects.create(
            category=food, name='Burger', price='25000',
            description='Original', branch_id='cloud',
        )
        result = CloudReceiver.receive_batch('product', 'branch-a', [{
            'uuid': str(product.uuid),
            'sync_version': product.sync_version + 500,
            'is_deleted': True,
            'name': 'Cheap forged burger',
            'description': 'Mutated',
            'price': '1',
            'category_uuid': str(forged_category.uuid),
        }])

        assert result['skipped'] == 1
        assert result['updated'] == 0
        product.refresh_from_db()
        assert product.name == 'Burger'
        assert product.description == 'Original'
        assert product.price == 25000
        assert product.category_id == food.id
        assert product.is_deleted is False
        assert product.sync_version < 500
