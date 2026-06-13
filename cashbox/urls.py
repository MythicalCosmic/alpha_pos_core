from django.urls import path

from cashbox import views

urlpatterns = [
    path('shifts/<int:shift_id>/expenses/', views.cashbox_expenses, name='cashbox-expenses'),
    path('categories/', views.cashbox_categories, name='cashbox-categories'),
    path('recipients/search/', views.recipient_search, name='cashbox-recipient-search'),
]
