from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from base.models import User, Category, Product, Order, OrderItem
from fiscalization.config import FiscalConfig
from fiscalization.models import FiscalReceipt
from fiscalization.providers import get_provider, MockProvider
from fiscalization.services import FiscalizationService
from fiscalization.services.builder import build_receipt_payload, _to_tiyin, _vat_portion


@pytest.fixture
def order(db):
    user = User.objects.create(first_name='T', last_name='U', email='t@u.local',
                               password='x', role='CASHIER', status='ACTIVE')
    cat = Category.objects.create(name='Food')
    p1 = Product.objects.create(category=cat, name='Lavash', price=Decimal('25000'),
                                ikpu_code='00803001001000000')
    p2 = Product.objects.create(category=cat, name='Cola', price=Decimal('10000'))
    o = Order.objects.create(
        user=user,
        cashier=user,
        status='READY',
        is_paid=True,
        paid_at=timezone.now(),
        total_amount=Decimal('60000'),
        payment_method='CASH',
    )
    OrderItem.objects.create(order=o, product=p1, quantity=2, price=Decimal('25000'))
    OrderItem.objects.create(order=o, product=p2, quantity=1, price=Decimal('10000'))
    return o


class TestBuilder:
    def test_money_to_tiyin(self):
        assert _to_tiyin(Decimal('25000')) == 2500000
        assert _to_tiyin(Decimal('25000.50')) == 2500050

    def test_vat_portion_inclusive(self):
        # 12% VAT-inclusive of 1,120,000 tiyin -> 120,000 tiyin
        assert _vat_portion(1120000, 12) == 120000

    def test_vat_zero_when_unregistered(self):
        assert _vat_portion(1120000, 0) == 0

    def test_payload_shape(self, order):
        tenant = {'tin': '123456789', 'vat_percent': 0}
        payload = build_receipt_payload(order, tenant)
        assert payload['tin'] == '123456789'
        assert payload['total'] == 6000000
        assert payload['received_cash'] == 6000000
        assert payload['received_card'] == 0
        assert len(payload['items']) == 2
        assert payload['items'][0]['price'] == 5000000  # 25000 * 2
        assert payload['items'][0]['ikpu'] == '00803001001000000'


class TestMockProvider:
    def test_deterministic_sign(self):
        p = MockProvider({'tin': '1'})
        payload = {'tin': '1', 'order_id': 7, 'receipt_type': 'SALE', 'total': 100,
                   'items': [{'name': 'x'}]}
        r1 = p.fiscalize(payload)
        r2 = p.fiscalize(payload)
        assert r1.success and r1.fiscal_sign == r2.fiscal_sign
        assert len(r1.fiscal_sign) == 12
        assert r1.qr_url.startswith('https://ofd.soliq.uz/')

    def test_rejects_empty_items(self):
        r = MockProvider({}).fiscalize({'items': []})
        assert not r.success

    def test_factory_falls_back_to_mock(self):
        assert isinstance(get_provider('nonsense', {}), MockProvider)


class TestToggle:
    def test_mode_override(self):
        FiscalConfig.set_mode('mock')
        assert FiscalConfig.is_enabled() and FiscalConfig.get_mode() == 'mock'
        FiscalConfig.set_mode('off')
        assert not FiscalConfig.is_enabled()

    def test_mock_mode_forces_mock_provider(self):
        with override_settings(FISCAL_PROVIDER='multikassa'):
            FiscalConfig.set_mode('mock')
            assert FiscalConfig.get_provider_name() == 'mock'
        FiscalConfig.set_mode('off')


@pytest.mark.django_db
class TestFiscalizeOrder:
    def test_disabled_skips(self, order):
        FiscalConfig.set_mode('off')
        result, status = FiscalizationService.fiscalize_order(order.id)
        assert result['success'] and result['data']['skipped'] is True
        assert not FiscalReceipt.objects.filter(order=order).exists()

    def test_mock_fiscalizes_and_is_idempotent(self, order):
        FiscalConfig.set_mode('mock')
        try:
            result, status = FiscalizationService.fiscalize_order(order.id)
            assert result['success']
            receipt = FiscalReceipt.objects.get(order=order, receipt_type='SALE')
            assert receipt.status == 'CONFIRMED'
            assert receipt.fiscal_sign and receipt.qr_url
            sign = receipt.fiscal_sign
            # Re-calling does not create a second receipt or change the sign.
            FiscalizationService.fiscalize_order(order.id)
            assert FiscalReceipt.objects.filter(order=order, receipt_type='SALE').count() == 1
            receipt.refresh_from_db()
            assert receipt.fiscal_sign == sign
        finally:
            FiscalConfig.set_mode('off')

    def test_unpaid_order_cannot_issue_a_sale_receipt(self, order):
        Order.objects.filter(pk=order.pk).update(is_paid=False, paid_at=None)
        FiscalConfig.set_mode('mock')
        try:
            result, status = FiscalizationService.fiscalize_order(order.id)
            assert status == 422
            assert result['success'] is False
            assert 'unpaid' in result['message'].lower()
            assert not FiscalReceipt.objects.filter(order=order).exists()
        finally:
            FiscalConfig.set_mode('off')

    def test_live_provider_unconfigured_fails_not_raises(self, order):
        with override_settings(FISCAL_PROVIDER='multikassa'):
            FiscalConfig.set_mode('sandbox')
            try:
                result, status = FiscalizationService.fiscalize_order(order.id)
                assert not result['success']
                receipt = FiscalReceipt.objects.get(order=order, receipt_type='SALE')
                assert receipt.status == 'FAILED'
                # Retry sweep re-attempts FAILED rows without raising.
                stats = FiscalizationService.retry_failed()
                assert stats['retried'] >= 1
            finally:
                FiscalConfig.set_mode('off')
