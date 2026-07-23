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
from django.db import IntegrityError, transaction

from base.services.sync.encoder import serialize_payload
from base.services.sync.evidence import emit_sync_evidence

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
        # A queue row is a mutable slot, but every distinct payload in that slot
        # has its own immutable generation token.  This is what makes a late ACK
        # safe: the sender can acknowledge the token it actually transmitted,
        # without deleting a newer edit that arrived while HTTP was in flight.
        #
        # Reset retry state only for genuinely new content.  Re-adding the same
        # poison payload (the reconcile sweep runs every cycle) must not revive
        # it forever; editing/correcting it should revive it immediately.
        operation = 'unchanged'
        for create_attempt in range(2):
            try:
                with transaction.atomic():
                    record = (
                        SyncQueueRecord.objects.select_for_update()
                        .filter(model_name=model_name, record_uuid=record_uuid)
                        .first()
                    )
                    if record is None:
                        record = SyncQueueRecord.objects.create(
                            model_name=model_name,
                            record_uuid=record_uuid,
                            payload=payload,
                        )
                        operation = 'created'
                    elif (_payload_version(payload) is not None
                          and _payload_version(record.payload) is not None
                          and _payload_version(payload)
                          < _payload_version(record.payload)):
                        # A reconcile/full-push snapshot can be serialized before
                        # a concurrent save queues a newer version, then reach
                        # add() afterwards. Never let that late stale writer
                        # replace the newer payload merely because content differs.
                        pass
                    elif record.payload != payload:
                        record.payload = payload
                        record.generation = uuid_module.uuid4()
                        record.attempts = 0
                        record.last_error = ''
                        record.save(update_fields=[
                            'payload', 'generation', 'attempts', 'last_error',
                            'updated_at',
                        ])
                        operation = 'replaced'
                break
            except IntegrityError:
                # Two first-time enqueue attempts can both observe an empty slot.
                # The unique (model_name, record_uuid) constraint picks a winner;
                # retry once and update/compare the winner under its row lock.
                if create_attempt:
                    raise
        logger.debug(f'Sync queued: {model_name} {uuid_val}')
        emit_sync_evidence(
            'queue_upsert', operation=operation, record=cls._to_dict(record),
        )
        return str(record.generation)

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
        qs = SyncQueueRecord.objects.exclude(
            last_error__startswith='[REJECTED]',
        ).exclude(
            last_error__startswith='[BRANCH_SCOPE]',
        )
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
        from django.db.models import Q
        dead = (
            Q(last_error__startswith='[REJECTED]')
            | Q(last_error__startswith='[BRANCH_SCOPE]')
        )
        if max_attempts:
            dead |= Q(attempts__gte=max_attempts)
        return SyncQueueRecord.objects.filter(dead).count()

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
        from django.db.models import Q

        total = SyncQueueRecord.objects.count()
        # A retryable batch/record deferral deliberately does not consume the
        # poison-message attempt budget, but it is still a failed/blocked queue
        # row that operators must see.  Counting only attempts>0 made the
        # dashboard report a clean queue while missing-dependency rows sat
        # indefinitely with a useful last_error.
        failed = SyncQueueRecord.objects.filter(
            Q(attempts__gt=0) | ~Q(last_error=''),
        ).count()
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
        removed = [cls._to_dict(row) for row in qs.iterator()]
        qs.delete()
        if removed:
            emit_sync_evidence('queue_removed', reason='explicit_remove', records=removed)

    @classmethod
    def acknowledge(cls, records, model_name):
        """Delete only queue rows whose generation was actually delivered.

        ``records`` are snapshots returned by :meth:`get_grouped`.  A save may
        replace the row between snapshot/send/ACK; in that case its generation
        no longer matches and the newer row deliberately remains queued.
        Returns the UUID strings whose exact generations were removed.
        """
        from base.models import SyncQueueRecord

        expected = cls._expected_generations(records)
        if not expected:
            return set()

        snapshots = []
        with transaction.atomic():
            rows = list(
                SyncQueueRecord.objects.select_for_update().filter(
                    model_name=model_name,
                    record_uuid__in=list(expected),
                )
            )
            matched = [
                row for row in rows
                if expected.get(row.record_uuid) == row.generation
            ]
            if matched:
                snapshots = [cls._to_dict(row) for row in matched]
                SyncQueueRecord.objects.filter(
                    pk__in=[row.pk for row in matched],
                ).delete()
        if snapshots:
            emit_sync_evidence(
                'queue_acknowledged', model_name=model_name, records=snapshots,
            )
        return {str(row.record_uuid) for row in matched}

    @classmethod
    def mark_failed(cls, uuid_val, error, model_name=None, generation=None):
        from base.models import SyncQueueRecord
        try:
            record_uuid = _coerce_uuid(uuid_val)
        except (ValueError, TypeError):
            return
        qs = SyncQueueRecord.objects.filter(record_uuid=record_uuid)
        if model_name is not None:
            qs = qs.filter(model_name=model_name)
        if generation is not None:
            try:
                qs = qs.filter(generation=_coerce_uuid(generation))
            except (ValueError, TypeError):
                return
        qs.update(
            attempts=models_F_plus_one(),
            last_error=str(error)[:500],
        )
        rows = [cls._to_dict(row) for row in qs.iterator()]
        if rows:
            emit_sync_evidence('queue_failed', error=str(error)[:500], records=rows)

    @classmethod
    def mark_batch_failed(cls, uuids, error, model_name=None, generations=None):
        """Consume one poison-record attempt for exact rejected generations.

        This is reserved for receiver responses which identify the individual
        UUIDs that could not be applied.  Transport/authentication/server-wide
        failures are not evidence that any record is poison and must use
        :meth:`mark_batch_deferred` instead; otherwise a short outage can
        dead-letter valid orders and payments permanently.
        """
        return cls._record_batch_error(
            uuids,
            error,
            model_name=model_name,
            generations=generations,
            consume_attempt=True,
        )

    @classmethod
    def mark_batch_deferred(cls, uuids, error, model_name=None, generations=None):
        """Retain a systemically blocked batch without poisoning its records.

        A 401 after token rotation, a 5xx deployment fault, timeout, or a legacy
        batch-level rejection applies to the delivery attempt as a whole.  It
        should remain observable in ``last_error`` but must not advance the
        per-record dead-letter counter: the exact same payload may be valid as
        soon as the shared dependency recovers.
        """
        return cls._record_batch_error(
            uuids,
            error,
            model_name=model_name,
            generations=generations,
            consume_attempt=False,
        )

    @classmethod
    def mark_batch_rejected(
        cls, uuids, error, model_name=None, generations=None,
    ):
        """Dead-letter exact generations explicitly rejected by the receiver."""
        from base.services.sync.config import get_sync_max_queue_attempts

        return cls._record_batch_error(
            uuids,
            f'[REJECTED] {error}',
            model_name=model_name,
            generations=generations,
            consume_attempt=False,
            force_attempts=max(1, get_sync_max_queue_attempts()),
        )

    @classmethod
    def revive_legacy_dead_letters(cls):
        """One-time revival after retryable dependency failures stopped poisoning.

        Old builds consumed attempts for missing parents and systemic outages.
        Replaying those rows once under ACK protocol v2 is safe; an actually
        invalid row is now explicitly rejected and immediately dead-lettered.
        """
        from base.models import SyncQueueRecord, SyncState
        from base.services.sync.config import get_sync_max_queue_attempts
        from base.services.sync.status import SyncStatus

        max_attempts = get_sync_max_queue_attempts()
        if not max_attempts:
            return 0
        marker_key = SyncStatus.dead_letter_revival_key()
        revived = []
        with transaction.atomic():
            marker, _ = SyncState.objects.select_for_update().get_or_create(
                key=marker_key, defaults={'value': ''},
            )
            if marker.value == 'complete':
                return 0
            rows = list(
                SyncQueueRecord.objects.select_for_update()
                .filter(attempts__gte=max_attempts)
                .exclude(last_error__startswith='[REJECTED]')
                .exclude(last_error__startswith='[BRANCH_SCOPE]')
            )
            if rows:
                SyncQueueRecord.objects.filter(
                    pk__in=[row.pk for row in rows],
                ).update(attempts=0, last_error='')
                revived = [cls._to_dict(row) for row in rows]
            marker.value = 'complete'
            marker.save(update_fields=['value', 'updated_at'])
        if revived:
            emit_sync_evidence(
                'queue_dead_letters_revived',
                reason='ack_protocol_v2_retry_classification',
                records=revived,
            )
        return len(revived)

    @classmethod
    def quarantine_foreign_branch_records(cls, branch_id):
        """Prevent stale branch-A payloads from authenticating as branch B."""
        from base.models import SyncQueueRecord
        from base.services.sync.config import get_sync_max_queue_attempts

        branch_id = str(branch_id or '').strip()
        if not branch_id:
            return 0
        cap = max(1, get_sync_max_queue_attempts())
        quarantined = []
        restored = []
        with transaction.atomic():
            rows = list(
                SyncQueueRecord.objects.select_for_update().all()
            )
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                payload_branch = str(payload.get('branch_id') or '').strip()
                if payload_branch and payload_branch != branch_id:
                    error = (
                        f'[BRANCH_SCOPE] payload belongs to {payload_branch}; '
                        f'current branch is {branch_id}'
                    )
                    if row.last_error != error or row.attempts != cap:
                        row.last_error = error[:500]
                        row.attempts = cap
                        row.save(update_fields=[
                            'last_error', 'attempts', 'updated_at',
                        ])
                    quarantined.append(cls._to_dict(row))
                elif row.last_error.startswith('[BRANCH_SCOPE]'):
                    row.last_error = ''
                    row.attempts = 0
                    row.save(update_fields=[
                        'last_error', 'attempts', 'updated_at',
                    ])
                    restored.append(cls._to_dict(row))
        if quarantined:
            emit_sync_evidence(
                'queue_branch_scope_quarantined',
                branch_id=branch_id,
                records=quarantined,
            )
        if restored:
            emit_sync_evidence(
                'queue_branch_scope_restored',
                branch_id=branch_id,
                records=restored,
            )
        return len(quarantined)

    @classmethod
    def _record_batch_error(
        cls,
        uuids,
        error,
        *,
        model_name=None,
        generations=None,
        consume_attempt,
        force_attempts=None,
    ):
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
        if generations is not None:
            expected = cls._expected_generations(
                ({'uuid': str(u), 'generation': generations.get(str(u))}
                 for u in coerced)
            )
            # Lock+compare each row: filtering UUIDs and generation tokens as
            # independent __in lists would allow cross-pair matches.
            with transaction.atomic():
                rows = list(qs.select_for_update())
                matched_pks = [
                    row.pk for row in rows
                    if expected.get(row.record_uuid) == row.generation
                ]
                if not matched_pks:
                    return set()
                updates = {'last_error': str(error)[:500]}
                if force_attempts is not None:
                    updates['attempts'] = force_attempts
                elif consume_attempt:
                    updates['attempts'] = models_F_plus_one()
                SyncQueueRecord.objects.filter(pk__in=matched_pks).update(**updates)
                failed_rows = list(
                    SyncQueueRecord.objects.filter(pk__in=matched_pks).iterator()
                )
                emit_sync_evidence(
                    'queue_failed', error=str(error)[:500],
                    failure_scope=('record' if consume_attempt else 'batch'),
                    attempts_consumed=consume_attempt,
                    force_attempts=force_attempts,
                    records=[cls._to_dict(row) for row in failed_rows],
                )
            return {
                str(row.record_uuid) for row in rows if row.pk in matched_pks
            }
        updates = {'last_error': str(error)[:500]}
        if force_attempts is not None:
            updates['attempts'] = force_attempts
        elif consume_attempt:
            updates['attempts'] = models_F_plus_one()
        qs.update(**updates)
        rows = [cls._to_dict(row) for row in qs.iterator()]
        if rows:
            emit_sync_evidence(
                'queue_failed', error=str(error)[:500],
                failure_scope=('record' if consume_attempt else 'batch'),
                attempts_consumed=consume_attempt, records=rows,
            )
        return {str(u) for u in coerced}

    @classmethod
    def clear(cls, *, include_tombstones=False):
        """Clear rebuildable queue slots without erasing deletion evidence.

        A live row removed from this cache is rediscovered by the unsynced-row
        reconciliation sweep.  A hard-delete tombstone has no source row left;
        its queue slot is the *only* durable record that tells the peer to
        remove the object.  The old blanket clear silently resurrected deleted
        order items on the cloud.  Preserve tombstones by default, while keeping
        an explicit internal escape hatch for full database reset workflows.
        Returns the number of rows removed.
        """
        from base.models import SyncQueueRecord
        qs = SyncQueueRecord.objects.all()
        if not include_tombstones:
            qs = qs.exclude(payload__is_deleted=True)
        rows = [cls._to_dict(row) for row in qs.iterator()]
        qs.delete()
        if rows:
            emit_sync_evidence(
                'queue_removed',
                reason=('clear_all' if include_tombstones else 'clear_rebuildable'),
                records=rows,
            )
        return len(rows)

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
            'generation': str(record.generation),
            'data': record.payload,
            'created_at': record.created_at.isoformat() if record.created_at else None,
            'attempts': record.attempts,
            'last_error': record.last_error or None,
        }

    @staticmethod
    def _expected_generations(records):
        expected = {}
        for record in records:
            try:
                record_uuid = _coerce_uuid(record.get('uuid'))
                generation = _coerce_uuid(record.get('generation'))
            except (AttributeError, ValueError, TypeError):
                continue
            expected[record_uuid] = generation
        return expected


def models_F_plus_one():
    # Local helper to keep the import small at module top.
    from django.db.models import F
    return F('attempts') + 1


def _payload_version(payload):
    try:
        value = payload.get('sync_version')
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
