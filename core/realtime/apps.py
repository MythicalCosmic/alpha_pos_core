from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    name = 'core.realtime'
    label = 'realtime'
    verbose_name = 'Realtime (websockets)'

    def ready(self):
        # Connect the Order broadcast signal once the app registry is ready.
        from core.realtime import signals  # noqa: F401
