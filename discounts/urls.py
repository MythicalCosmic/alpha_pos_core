from django.urls import path
from discounts.views import discount_views

app_name = 'discounts'

urlpatterns = [
    path('types/', discount_views.discount_types, name='type-list'),
    path('types/<int:type_id>/', discount_views.discount_type_detail, name='type-detail'),
    path('discounts/', discount_views.discounts, name='discount-list'),
    path('discounts/<int:discount_id>/', discount_views.discount_detail, name='discount-detail'),
    path('discounts/<int:discount_id>/toggle/', discount_views.discount_toggle, name='discount-toggle'),
    path('discounts/<int:discount_id>/stats/', discount_views.discount_stats, name='discount-stats'),
    path('validate/', discount_views.validate_discount, name='validate'),
    path('apply/', discount_views.apply_discount, name='apply'),
    path('remove/', discount_views.remove_discount, name='remove'),
    path('secret-word/', discount_views.validate_secret_word, name='secret-word'),
]
