"""ASGI for core's own runs (editions ship their own config.asgi)."""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alpha_pos_core.settings_base')

from django.core.asgi import get_asgi_application

# Initialise Django (populate the app registry) BEFORE importing consumers.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

from core.realtime.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
})
