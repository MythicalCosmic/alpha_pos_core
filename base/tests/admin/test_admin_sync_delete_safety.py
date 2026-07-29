from django.contrib import admin
from django.test import override_settings

from base.models import Category, SyncQueueRecord


def test_default_admin_bulk_delete_action_is_globally_disabled():
    assert 'delete_selected' not in admin.site.actions


@override_settings(
    DEPLOYMENT_MODE='local',
    BRANCH_ID='admin-delete-safety',
    SYNC_ENABLED=True,
)
def test_individual_admin_delete_uses_sync_soft_delete(db):
    category = Category.objects.create(name='Admin delete safety')
    SyncQueueRecord.objects.all().delete()
    starting_version = category.sync_version

    model_admin = admin.site._registry[Category]
    model_admin.delete_model(request=None, obj=category)

    category.refresh_from_db()
    assert category.is_deleted is True
    assert category.sync_version == starting_version + 1
    queued = SyncQueueRecord.objects.get(
        model_name='category',
        record_uuid=category.uuid,
    )
    assert queued.payload['is_deleted'] is True
    assert queued.payload['sync_version'] == category.sync_version
