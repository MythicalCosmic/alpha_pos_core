"""`resync_dead_letters` requeues rows that hit the dead-letter cap so a shift/
order that went permanently missing on the cloud gets a fresh chance to push,
without disturbing rows that are still within their normal retry budget."""
import uuid as uuidlib
from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def _rec(model_name, attempts):
    from base.models import SyncQueueRecord
    return SyncQueueRecord.objects.create(
        model_name=model_name, record_uuid=uuidlib.uuid4(),
        payload={'x': 1}, attempts=attempts, last_error='boom')


def _cap():
    from base.services.sync.config import get_sync_max_queue_attempts
    return get_sync_max_queue_attempts()


def test_resets_dead_lettered_only():
    cap = _cap()
    dead = _rec('shift', cap)        # at the cap => dead-lettered
    healthy = _rec('order', 1)       # within budget => must be left alone
    out = StringIO()
    call_command('resync_dead_letters', stdout=out)
    dead.refresh_from_db()
    healthy.refresh_from_db()
    assert dead.attempts == 0
    assert dead.last_error == '[RETRYING] boom'
    assert healthy.attempts == 1 and healthy.last_error == 'boom'
    assert 'shift' in out.getvalue()


def test_model_filter_scopes_the_reset():
    cap = _cap()
    shift = _rec('shift', cap)
    order = _rec('order', cap)
    call_command('resync_dead_letters', '--model', 'shift', stdout=StringIO())
    shift.refresh_from_db()
    order.refresh_from_db()
    assert shift.attempts == 0
    assert order.attempts == cap      # other model untouched


def test_dry_run_writes_nothing():
    cap = _cap()
    dead = _rec('shift', cap)
    out = StringIO()
    call_command('resync_dead_letters', '--dry-run', stdout=out)
    dead.refresh_from_db()
    assert dead.attempts == cap       # unchanged
    assert 'dry-run' in out.getvalue().lower()


def test_no_dead_letters_is_a_noop():
    _rec('order', 1)                  # nothing at the cap
    out = StringIO()
    call_command('resync_dead_letters', stdout=out)
    assert 'No dead-lettered' in out.getvalue()


def test_explicit_rejection_remains_recoverable_when_cap_is_disabled(settings):
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 0
    row = _rec('orderpayment', 0)
    row.last_error = '[REJECTED] PAYMENT_CONFLICT: amount differs'
    row.save(update_fields=['last_error'])

    call_command('resync_dead_letters', stdout=StringIO())

    row.refresh_from_db()
    assert row.attempts == 0
    assert row.last_error == (
        '[RETRYING] [REJECTED] PAYMENT_CONFLICT: amount differs'
    )


def test_repeated_command_keeps_original_rejection_and_drops_old_push_symptom(
    settings,
):
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 3
    row = _rec('order', 0)
    row.last_error = (
        '[RETRYING] [REJECTED] invalid settlement evidence'
        ' | latest push: HTTP 401'
    )
    row.save(update_fields=['last_error'])

    call_command('resync_dead_letters', stdout=StringIO())

    row.refresh_from_db()
    assert row.last_error == (
        '[RETRYING] [REJECTED] invalid settlement evidence'
    )
