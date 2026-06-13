from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class LicensingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'licensing'

    def ready(self):
        # The kill switch only works if our middleware actually runs. A
        # future refactor could easily reorder MIDDLEWARE and silently
        # break enforcement. Assert at boot so the test suite + `manage.py
        # check` both catch a misconfiguration before it ships.
        from django.conf import settings

        target = 'licensing.middleware.LicenseEnforcementMiddleware'
        middleware = list(getattr(settings, 'MIDDLEWARE', []))
        if target not in middleware:
            raise ImproperlyConfigured(
                f'{target} must be in MIDDLEWARE for the license kill '
                f'switch to take effect.'
            )

        # Must run after CorsMiddleware so 503 responses get CORS headers
        # (otherwise the Electron renderer can't read the license_inactive
        # body and just sees an opaque CORS error). Must run before
        # SessionMiddleware so we never spin up auth machinery for a
        # request we're about to refuse.
        try:
            cors_idx = middleware.index('corsheaders.middleware.CorsMiddleware')
        except ValueError:
            cors_idx = None
        license_idx = middleware.index(target)
        if cors_idx is not None and license_idx < cors_idx:
            raise ImproperlyConfigured(
                'LicenseEnforcementMiddleware must come AFTER CorsMiddleware '
                'so 503 responses carry CORS headers.'
            )
