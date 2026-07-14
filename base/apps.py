from django.apps import AppConfig


class BaseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'base'

    def ready(self):
        # Wire signal receivers (currently: drop cached Session rows when
        # their DB row is deleted from any path, not just the auth-service
        # logout flow). Imported here so the signal handlers register at
        # app boot.
        from base import signals  # noqa: F401

        # Reset the local pull scope/cursor once the upgraded schema exists.
        # Connect to every app's post_migrate because base may finish before a
        # branch-scoped child app table; the epoch method is idempotent/retries.
        from django.db.models.signals import post_migrate

        def ensure_sync_scope_epoch(**_kwargs):
            from base.services.sync.status import SyncStatus
            SyncStatus.ensure_scope_epoch()

        post_migrate.connect(
            ensure_sync_scope_epoch,
            dispatch_uid='base.ensure_sync_scope_epoch',
            weak=False,
        )
