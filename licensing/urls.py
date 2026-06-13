from django.urls import path

from licensing import views


urlpatterns = [
    path('status', views.status_view, name='licensing-status'),
    path('setup', views.setup_view, name='licensing-setup'),
    path('plans', views.plans_view, name='licensing-plans'),
    path('plan-change', views.plan_change_view, name='licensing-plan-change'),
]
