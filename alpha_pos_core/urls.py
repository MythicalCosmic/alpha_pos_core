"""Minimal URLconf for core's OWN manage.py (check / makemigrations / tests).

Editions ship their own ``config.urls``; core only needs something importable so
its shared apps can be checked/migrated in isolation. Only shared-app routes here.
"""
from django.contrib import admin
from django.urls import path, include

from base.services.sync.views import get_sync_urls

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/sync/', include(get_sync_urls())),
    path('api/licensing/', include('licensing.urls')),
]
