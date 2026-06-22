from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    def ready(self):
        # Registers both the chat-config sync (any edition) and the staff order
        # trigger (which self-gates to the server edition inside the receiver).
        from notifications import signals  # noqa: F401
