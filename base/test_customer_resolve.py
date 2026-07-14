"""Unified client identity: Customer.resolve() phone/telegram reconciliation +
the customer-bot contact-share capture that feeds it."""
import pytest
from django.test import override_settings

from base.models import Customer

pytestmark = pytest.mark.django_db


def test_normalize_phone_variants_collapse():
    n = Customer.normalize_phone
    assert n('+998 90 123-45-67') == n('998901234567') == n('901234567') == '998901234567'
    assert n('') == '' and n(None) == ''


def test_resolve_creates_when_new():
    c, created = Customer.resolve(phone='998901112233', name='Ali')
    assert created and c.phone_number == '998901112233' and c.name == 'Ali'


def test_resolve_converges_telegram_onto_instore_phone_row():
    # In-store walk-in created by phone on the desktop (no telegram).
    instore = Customer.objects.create(name='Walk In', phone_number='998901112233')
    # Same person later logs into the bot + shares the same number (telegram_id set).
    same, created = Customer.resolve(phone='+998 90 111 22 33', telegram_id=55501, name='Ali')
    assert not created
    assert same.id == instore.id                 # converged, not a 2nd row
    same.refresh_from_db()
    assert same.telegram_id == 55501             # telegram_id backfilled onto the in-store row
    assert Customer.objects.filter(phone_number__contains='111').count() == 1


def test_resolve_matches_telegram_when_no_phone_match():
    bot = Customer.objects.create(name='Bot User', telegram_id=42)
    same, created = Customer.resolve(telegram_id=42)
    assert not created and same.id == bot.id


def test_resolve_backfills_name_without_clobbering():
    c = Customer.objects.create(phone_number='998905556677', name='Existing')
    again, _ = Customer.resolve(phone='998905556677', name='Should Not Overwrite')
    again.refresh_from_db()
    assert again.id == c.id and again.name == 'Existing'   # existing name kept


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_branch_agnostic_link_does_not_create_cloud_owned_customer():
    customer, created = Customer.resolve(
        phone='998901010101', telegram_id=101, create=False,
    )

    assert customer is None
    assert created is False
    assert not Customer.objects.filter(phone_number='998901010101').exists()


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_dispatch_scope_adopts_old_cloud_placeholder_into_target_branch():
    placeholder = Customer.objects.create(
        name='Bot Customer', phone_number='998902020202', branch_id='cloud',
    )

    customer, created = Customer.resolve(
        phone='+998 90 202 02 02', telegram_id=202,
        branch_id='branch-a', adopt_node_owned=True,
    )

    assert created is False
    assert customer.id == placeholder.id
    customer.refresh_from_db()
    assert customer.branch_id == 'branch-a'
    assert customer.telegram_id == 202


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_dispatch_scope_never_steals_customer_from_another_branch():
    foreign = Customer.objects.create(
        phone_number='998903030303', branch_id='branch-b',
    )

    customer, created = Customer.resolve(
        phone='998903030303', telegram_id=303,
        branch_id='branch-a', adopt_node_owned=True,
    )

    assert created is True
    assert customer.id != foreign.id
    assert customer.branch_id == 'branch-a'
    foreign.refresh_from_db()
    assert foreign.branch_id == 'branch-b'
    assert foreign.telegram_id is None


def test_bot_contact_capture_resolves_customer():
    from notifications.services import customer_bot
    update = {'message': {
        'chat': {'id': 70001},
        'from': {'id': 70001},
        'contact': {'phone_number': '998901234567', 'first_name': 'Vali',
                    'last_name': 'Aliyev', 'user_id': 70001},
    }}
    phone = customer_bot._capture_contact(update, 70001)
    assert phone == '998901234567'
    c = Customer.objects.get(telegram_id=70001)
    assert c.phone_number == '998901234567' and c.name == 'Vali Aliyev'
    assert customer_bot._has_phone(70001) is True


def test_bot_ignores_contact_about_someone_else():
    from notifications.services import customer_bot
    update = {'message': {'chat': {'id': 80001}, 'from': {'id': 80001},
                          'contact': {'phone_number': '998900000000', 'user_id': 99999}}}
    assert customer_bot._capture_contact(update, 80001) is None
    assert not Customer.objects.filter(telegram_id=80001).exists()
