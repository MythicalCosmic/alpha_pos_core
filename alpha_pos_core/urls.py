"""URLconf for core's own manage.py + the SHARED-app test suite.

The editions ship their own ``config.urls`` (server = back-office, local = POS).
This one mounts the full HTTP surface of the apps that live in core, so
``cd alpha_pos_core && pytest`` exercises the shared apps' endpoints (the test
files are HTTP/APITestCase tests that need their routes mounted).

``customers`` / ``waiters`` / ``admins`` are NOT installed in core, so their urls
are intentionally absent — their tests run on their own edition.
"""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include

from base.services.sync.views import get_sync_urls
from notifications.views import telegram_views, qr_order_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('healthz', lambda _r: HttpResponse('ok', content_type='text/plain')),
    path('api/admins/stock/', include('stock.urls')),
    path('api/admins/hr/', include('hr.urls')),
    path('api/admins/discounts/', include('discounts.urls')),
    path('api/admins/notifications/', include('notifications.urls')),
    path('api/admins/cashbox/', include('cashbox.urls')),
    path('api/sync/', include(get_sync_urls())),
    path('api/licensing/', include('licensing.urls')),
    path('api/fiscalization/', include('fiscalization.urls')),
    path('api/telegram/webhook/', telegram_views.webhook, name='telegram-webhook'),
    path('api/qr/menu/<str:token>/', qr_order_views.menu_view, name='qr-menu'),
    path('api/qr/order/<str:token>/', qr_order_views.order_view, name='qr-order'),
]
