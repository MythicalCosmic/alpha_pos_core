from django.urls import path

from fiscalization import views


urlpatterns = [
    path('status', views.status_view, name='fiscal-status'),
    path('mode', views.set_mode_view, name='fiscal-set-mode'),
    path('receipts', views.list_view, name='fiscal-list'),
    path('retry', views.retry_view, name='fiscal-retry'),
    path('test', views.test_view, name='fiscal-test'),
    path('orders/<int:order_id>/fiscalize', views.fiscalize_view, name='fiscal-fiscalize'),
]
