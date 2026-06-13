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
