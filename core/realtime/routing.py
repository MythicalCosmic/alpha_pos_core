"""Websocket URL routing for the realtime consumers. Mounted by each edition's
config/asgi.py inside a ProtocolTypeRouter."""
from django.urls import re_path

from core.realtime import consumers

websocket_urlpatterns = [
    re_path(r'^ws/orders/$', consumers.OrderQueueConsumer.as_asgi()),
    re_path(r'^ws/kds/$', consumers.KdsConsumer.as_asgi()),
    re_path(r'^ws/cashiers/$', consumers.CashierControlConsumer.as_asgi()),
]
