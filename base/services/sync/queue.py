"""DB-backed sync queue.

Replaces the previous cache-backed queue. The cache implementation lost
records on process restart (LocMem default) and on Redis crashes between
flushes; this version persists every queued record in the SyncQueueRecord
table and only deletes on confirmed sync.

Public API is unchanged so existing callers (SyncService, SyncMixin,
management commands) continue to work without edits.
"""
import json
import logging
import uuid as uuid_module

from collections import defaultdict
from django.db import transaction

from base.services.sync.encoder import serialize_payload

logger = logging.getLogger(__name__)


def _coerce_uuid(value):
    if isinstance(value, uuid_module.UUID):
        return value
    return uuid_module.UUID(str(value))


def _serialize_data_field(payload):
    # SyncQueueRecord.payload is JSONField; serialize_payload returns a
    # JSON string, but the field expects a Python object — round-trip it
    # through json.loads so Decimal/datetime/UUID get normalized to the
    # JSON-safe representation used everywhere else.
    encoded = serialize_payload(payload)
    if isinstance(encoded, str):
        return json.loads(encoded)
    return encoded


class SyncQueue:

    @classmethod
    def add(cls, model_name, uuid_val, data):
        from base.models import SyncQueueRecord
        record_uuid = _coerce_uuid(uuid_val)
        payload = _serialize_data_field(data)
        with transaction.atomic():
            SyncQueueRecord.objects.update_or_create(
                model_name=model_name,
                record_uuid=record_uuid,
                defaults={'payload': payload, 'last_error': ''},
            )
        logger.debug(f'Sync queued: {model_name} {uuid_val}')

    @classmethod
    def get_all(cls):
        from base.models import SyncQueueRecord
        return [cls._to_dict(r) for r in SyncQueueRecord.objects.all().iterator()]

    @classmethod
    def get_grouped(cls):
        # Source of the outbound batch. Excludes dead-lettered records (attempts
        # at/over the cap) so a permanently-rejected row stops being retried
        # every cycle instead of spinning forever and blocking healthy records.
        from base.models import SyncQueueRecord
        from base.services.sync.config import get_sync_max_queue_attempts
        max_attempts = get_sync_max_queue_attempts()
        qs = SyncQueueRecord.objects.all()
        if max_attempts:
            qs = qs.filter(attempts__lt=max_attempts)
        grouped = defaultdict(list)
        for r in qs.iterator():
            grouped[r.model_name].append(cls._to_dict(r))
        return dict(grouped)

    @classmethod
    def dead_letter_count(cls):
        from base.models import SyncQueueRecord
        from base.services.sync.config import get_sync_max_queue_attempts
        max_attempts = get_sync_max_queue_attempts()
        if not max_attempts:
            return 0
        return SyncQueueRecord.objects.filter(attempts__gte=max_attempts).count()

    @classmethod
    def queued_uuids_for_model(cls, model_name):
        from base.models import SyncQueueRecord
        return {
            str(u) for u in SyncQueueRecord.objects.filter(
                model_name=model_name,
            ).values_list('record_uuid', flat=True)
        }

    @classmethod
    def count(cls):
        from base.models import SyncQueueRecord
        total = SyncQueueRecord.objects.count()
        failed = SyncQueueRecord.objects.filter(attempts__gt=0).count()
        return total, failed

    @classmethod
    def remove(cls, uuids, model_name=None):
        # The queue's unique key is (model_name, record_uuid): two different
        # models can legitimately hold the same record_uuid. Scope by model_name
        # when the caller knows it so we never delete a sibling model's row that
        # happens to share a uuid. model_name stays optional for back-compat.
        from base.models import SyncQueueRecord
        coerced = []
        for u in uuids:
            try:
                coerced.append(_coerce_uuid(u))
            except (ValueError, TypeError):
                continue
        if not coerced:
            return
        qs = SyncQueueRecord.objects.filter(record_uuid__in=coerced)
        if model_name is not None:
            qs = qs.filter(model_name=model_name)
        qs.delete()

    @classmethod
    def mark_failed(cls, uuid_val, error, model_name=None):
        from base.models import SyncQueueRecord
        try:
            record_uuid = _coerce_uuid(uuid_val)
        except (ValueError, TypeError):
            return
        qs = SyncQueueRecord.objects.filter(record_uuid=record_uuid)
        if model_name is not None:
            qs = qs.filter(model_name=model_name)
        qs.update(
            attempts=models_F_plus_one(),
            last_error=str(error)[:500],
        )

    @classmethod
    def mark_batch_failed(cls, uuids, error, model_name=None):
        # Scope by model_name (the unique key's other half) when known so a
        # failure on one model doesn't bump attempts on a different model's row
        # sharing the same record_uuid.
        from base.models import SyncQueueRecord
        coerced = []
        for u in uuids:
            try:
                coerced.append(_coerce_uuid(u))
            except (ValueError, TypeError):
                continue
        if not coerced:
            return
        qs = SyncQueueRecord.objects.filter(record_uuid__in=coerced)
        if model_name is not None:
            qs = qs.filter(model_name=model_name)
        qs.update(
            attempts=models_F_plus_one(),
            last_error=str(error)[:500],
        )

    @classmethod
    def clear(cls):
        from base.models import SyncQueueRecord
        SyncQueueRecord.objects.all().delete()

    @classmethod
    def get_summary(cls):
        from base.models import SyncQueueRecord
        from django.db.models import Count
        rows = SyncQueueRecord.objects.values('model_name').annotate(n=Count('id'))
        return {row['model_name']: row['n'] for row in rows}

    @classmethod
    def _to_dict(cls, record):
        return {
            'model_name': record.model_name,
            'uuid': str(record.record_uuid),
            'data': record.payload,
            'created_at': record.created_at.isoformat() if record.created_at else None,
            'attempts': record.attempts,
            'last_error': record.last_error or None,
        }


def models_F_plus_one():
    # Local helper to keep the import small at module top.
    from django.db.models import F
    return F('attempts') + 1
