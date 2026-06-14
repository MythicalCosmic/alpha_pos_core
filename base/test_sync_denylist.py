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
