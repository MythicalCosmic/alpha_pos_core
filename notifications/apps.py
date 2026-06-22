from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    def ready(self):
        from django.conf import settings
        # Staff order notifications are the SERVER's job (single source as orders
        # sync up). Register the post_save(Order) trigger only on the server so
        # the tills don't each fire their own copy.
        if getattr(settings, 'EDITION', '') == 'server':
            from notifications import signals  # noqa: F401
