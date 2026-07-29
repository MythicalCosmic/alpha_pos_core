"""Regression coverage for explicit relationship clearing over sync."""

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _shift_with_template():
    from base.models import Shift, ShiftTemplate, User

    user = User.objects.create(
        first_name='Sync', last_name='Cashier',
        email='sync-fk-clear@example.com', password='!', role='CASHIER',
    )
    template = ShiftTemplate.objects.create(
        name='Morning', start_time='08:00', end_time='16:00',
    )
    shift = Shift.objects.create(
        user=user, shift_template=template, start_time=timezone.now(),
        sync_version=1, branch_id='branch1',
    )
    return shift


def test_pull_explicit_null_clears_nullable_fk():
    from base.models import Shift

    shift = _shift_with_template()
    payload = shift.to_sync_dict()
    payload['sync_version'] = 2
    payload['shift_template_uuid'] = None

    instance, action = Shift.from_sync_dict(payload, branch_id='branch1')

    assert action == 'updated'
    instance.refresh_from_db()
    assert instance.shift_template_id is None


def test_push_explicit_null_clears_nullable_fk():
    from base.models import Shift
    from base.services.sync.receiver import CloudReceiver

    shift = _shift_with_template()
    payload = shift.to_sync_dict()
    payload['sync_version'] = 2
    payload['shift_template_uuid'] = None

    instance, action = CloudReceiver._create_or_update(
        Shift, payload, branch_id='branch1',
    )

    assert action == 'updated'
    instance.refresh_from_db()
    assert instance.shift_template_id is None


def test_pull_rejects_explicit_null_for_required_fk():
    from base.models import Place, Table

    place = Place.objects.create(name='Hall')
    table = Table.objects.create(place=place, number='1', sync_version=1)
    payload = table.to_sync_dict()
    payload['sync_version'] = 2
    payload['place_uuid'] = None

    instance, action = Table.from_sync_dict(
        payload, branch_id=payload['branch_id'],
    )

    assert instance is None
    assert action == 'deferred'
    table.refresh_from_db()
    assert table.place_id == place.id


def test_push_rejects_explicit_null_for_required_fk():
    from base.models import Place, Table
    from base.services.sync.receiver import CloudReceiver

    place = Place.objects.create(name='Hall', branch_id='branch1')
    table = Table.objects.create(
        place=place, number='1', sync_version=1, branch_id='branch1',
    )
    payload = table.to_sync_dict()
    payload['sync_version'] = 2
    payload['place_uuid'] = None

    with pytest.raises(ValueError, match='Unresolved required FK'):
        CloudReceiver._create_or_update(Table, payload, branch_id='branch1')

    table.refresh_from_db()
    assert table.place_id == place.id


def test_pull_defers_unknown_nullable_parent_instead_of_losing_link():
    import uuid
    from base.models import Shift

    shift = _shift_with_template()
    payload = shift.to_sync_dict()
    payload['sync_version'] = 2
    payload['shift_template_uuid'] = str(uuid.uuid4())

    instance, action = Shift.from_sync_dict(payload, branch_id='branch1')

    assert instance is None
    assert action == 'deferred'
    shift.refresh_from_db()
    assert shift.shift_template_id is not None


def test_push_defers_unknown_nullable_parent_instead_of_losing_link():
    import uuid
    from base.models import Shift
    from base.services.sync.receiver import CloudReceiver

    shift = _shift_with_template()
    payload = shift.to_sync_dict()
    payload['sync_version'] = 2
    payload['shift_template_uuid'] = str(uuid.uuid4())

    with pytest.raises(ValueError, match='Unresolved nullable FK'):
        CloudReceiver._create_or_update(Shift, payload, branch_id='branch1')

    shift.refresh_from_db()
    assert shift.shift_template_id is not None


def test_tombstone_can_delete_existing_child_when_parent_is_missing():
    import uuid
    from base.models import Place, Table
    from base.services.sync.receiver import CloudReceiver

    place = Place.objects.create(name='Temporary hall')
    table = Table.objects.create(
        place=place, number='9', sync_version=1, branch_id='branch1',
    )
    payload = table.to_sync_dict()
    payload.update({
        'sync_version': 2,
        'is_deleted': True,
        'place_uuid': str(uuid.uuid4()),
    })

    instance, action = CloudReceiver._create_or_update(
        Table, payload, branch_id='branch1',
    )

    assert action == 'updated'
    instance.refresh_from_db()
    assert instance.is_deleted is True
