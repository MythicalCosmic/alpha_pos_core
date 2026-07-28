from base.admin import OrderAdmin


def test_django_admin_cannot_mutate_derived_order_payment_header():
    assert {
        'is_paid', 'payment_method', 'paid_at', 'accounting_recorded_at',
    }.issubset(set(OrderAdmin.readonly_fields))
