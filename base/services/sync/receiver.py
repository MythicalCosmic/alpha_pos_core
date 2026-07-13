import logging
from decimal import Decimal
from django.apps import apps
from django.db import transaction
from django.utils import timezone
from base.services.sync.config import FK_UUID_MAPPINGS

logger = logging.getLogger(__name__)


def _resolve_foreign_keys(data):
    """Resolve UUID-keyed FK references to local PKs.

    Returns (resolved, missing) where:
      resolved: {fk_field: instance} for FKs successfully looked up
      missing:  [(uuid_field, uuid_value)] for FKs that referenced an unknown
                UUID. The caller decides whether to defer the record (for
                non-nullable FKs the row is incomplete) or persist with NULL
                (the legacy behavior — kept for nullable FKs).
    """
    resolved = {}
    missing = []
    for uuid_field, (app_label, model_name, fk_field) in FK_UUID_MAPPINGS.items():
        uuid_value = data.get(uuid_field)
        if not uuid_value:
            continue
        try:
            related_model = apps.get_model(app_label, model_name)
            instance = related_model.objects.filter(uuid=uuid_value).first()
            if instance:
                resolved[fk_field] = instance
            else:
                logger.warning(f'FK not found: {model_name} uuid={uuid_value}')
                missing.append((uuid_field, uuid_value))
        except Exception as e:
            logger.error(f'FK resolve error {uuid_field}: {e}')
            missing.append((uuid_field, uuid_value))
    return resolved, missing


def _parse_temporal(field_type, value):
    """Turn an incoming ISO date/datetime string into a real date/datetime.

    Sync payloads carry temporal fields as ISO strings (the encoder uses
    ``.isoformat()``). Django's stdlib parsers handle that with no third-party
    dependency. The old path did ``from dateutil import parser`` unconditionally;
    when dateutil isn't installed (it isn't in the server image) the caller
    swallowed the ImportError and stored the *raw string*. Postgres accepts a
    tidy ISO string but rejects the odd ones, so that row failed to write,
    retried to the dead-letter cap, and went permanently missing on the cloud —
    the mechanism behind the "missing shift" reports. Parse deterministically
    here so every well-formed value ingests; only fall through to dateutil (if
    present) or the raw value for something the stdlib can't read.
    """
    from django.utils.dateparse import parse_datetime, parse_date
    if field_type == 'DateField':
        parsed = parse_date(value)
        if parsed is None:
            dt = parse_datetime(value)
            parsed = dt.date() if dt is not None else None
    else:
        parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    try:
        from dateutil import parser as date_parser
        return date_parser.parse(value)
    except Exception:
        return value


def _clean_field_value(field, value):
    if value is None:
        return None

    field_type = field.get_internal_type()

    if field_type == 'DecimalField':
        return Decimal(str(value)) if value else Decimal('0')

    if field_type in ('DateTimeField', 'DateField'):
        if isinstance(value, str) and value:
            return _parse_temporal(field_type, value)
        return value

    if field_type == 'BooleanField':
        return bool(value)

    if field_type in ('IntegerField', 'PositiveIntegerField'):
        return int(value) if value is not None else None

    return value


def _prepare_fields(model_class, data):
    # Clean+coerce incoming scalar fields. The direction-aware write denylist is
    # applied later, per create/update branch, via model_class._strip_sync_denied
    # so a required column on a brand-new row isn't stripped (which would raise a
    # NOT NULL IntegrityError and re-queue the record forever).
    model_fields = {}
    for f in model_class._meta.get_fields():
        if hasattr(f, 'column'):
            model_fields[f.name] = f

    cleaned = {}
    for key, value in data.items():
        if key not in model_fields:
            continue
        field = model_fields[key]
        if field.get_internal_type() == 'ForeignKey':
            continue
        try:
            cleaned[key] = _clean_field_value(field, value)
        except Exception as e:
            logger.warning(f'Field {key} clean error: {e}')
            cleaned[key] = value

    return cleaned


def _strip_denied(model_class, cleaned, *, creating):
    # Delegate to the model's direction-aware policy when available (every
    # SyncMixin subclass has it). Non-SyncMixin models pass through unchanged.
    strip = getattr(model_class, '_strip_sync_denied', None)
    if strip is None:
        return cleaned
    return strip(cleaned, creating=creating)


def _preserve_updated_at(model_class, instance, incoming_updated):
    # .update() bypasses auto_now so the source-of-truth updated_at survives
    # the receive write and the _should_replace tiebreaker stays meaningful.
    if incoming_updated is None or not hasattr(instance, 'updated_at'):
        return
    model_class.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
    instance.updated_at = incoming_updated


def _preserve_created_at(model_class, instance, raw_created):
    # created_at is auto_now_add on every SyncMixin model, so save() stamps the
    # RECEIVER's clock on INSERT. When an offline till syncs a backlog (e.g. a whole
    # day worked while the cloud was down), every row would otherwise land with
    # TODAY's created_at — dumping yesterday's sales into today and corrupting all
    # time-series analytics (revenue-by-day, "today", business-day windows).
    # .update() bypasses auto_now_add so the origin created_at from the pushing
    # branch survives. No-op on UPDATE (auto_now_add only fires on INSERT), but
    # harmless there and it also corrects a row whose created_at was mis-stamped
    # by an earlier (buggy) receive.
    from django.utils.dateparse import parse_datetime
    if not raw_created or not hasattr(instance, 'created_at'):
        return
    val = parse_datetime(raw_created) if isinstance(raw_created, str) else raw_created
    if val is None:
        return
    model_class.objects.filter(pk=instance.pk).update(created_at=val)
    instance.created_at = val


class CloudReceiver:

    @classmethod
    def receive_batch(cls, model_name, branch_id, records):
        result = {
            'success': True,
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'errors': [],
            # UUIDs of records that raised during apply. Surfaced to the pusher
            # so it removes ONLY confirmed records from its durable queue and
            # re-queues the failures — otherwise a partial-failure batch was
            # purged wholesale on the HTTP-200, silently losing the bad rows.
            'failed_uuids': [],
        }

        try:
            if '.' in model_name:
                # Explicit 'app.Model'.
                app_label, model = model_name.split('.', 1)
                model_class = apps.get_model(app_label, model)
            else:
                # Bare lowercase name as queued by SyncService.queue_record
                # (instance.__class__.__name__.lower()). Resolve via the sync
                # registry so non-base apps (cashbox/stock/hr/discounts) map to
                # the RIGHT app — the old `else 'base'` default sent every bare
                # name to base and rejected all non-base records.
                from base.services.sync.config import resolve_model
                model_class = resolve_model(model_name)
                if model_class is None:
                    model_class = apps.get_model('base', model_name)  # legacy fallback
        except Exception as e:
            return {'success': False, 'created': 0, 'updated': 0, 'skipped': 0, 'errors': [str(e)]}

        # Per-model opt-out: AuditLog (and any future write-once-from-local
        # model) sets `_sync_ingest_disabled = True` so a peer can't push
        # forged rows. Push-side is unaffected — local writes still queue
        # outbound for the cloud.
        if getattr(model_class, '_sync_ingest_disabled', False):
            logger.info(
                'sync receive: ingest disabled for %s — skipping %d record(s)',
                model_class.__name__, len(records),
            )
            result['skipped'] = len(records)
            return result

        model_label = model_class.__name__
        affected_order_ids = set()
        for record_data in records:
            try:
                instance, action = cls._create_or_update(model_class, record_data, branch_id)
                if action == 'created':
                    result['created'] += 1
                elif action == 'updated':
                    result['updated'] += 1
                else:
                    result['skipped'] += 1
                # Collect the orders touched by this batch so staff notifications
                # fire AFTER the order + its items are all applied (items arrive
                # in a separate batch after the order — see _notify_received_orders).
                if instance is not None and action in ('created', 'updated'):
                    if model_label == 'Order':
                        affected_order_ids.add(instance.id)
                    elif model_label == 'OrderItem' and instance.order_id:
                        affected_order_ids.add(instance.order_id)
            except Exception as e:
                rec_uuid = record_data.get("uuid")
                error_msg = f'{rec_uuid or "?"}: {str(e)}'
                result['errors'].append(error_msg)
                if rec_uuid:
                    result['failed_uuids'].append(rec_uuid)
                logger.error(f'Receive error: {error_msg}')

        if affected_order_ids:
            cls._notify_received_orders(affected_order_ids)

        return result

    @staticmethod
    def _notify_received_orders(order_ids):
        """Server-only: fire the staff order notifications for orders touched by a
        just-applied sync batch. Runs after the batch loop (records are committed),
        so by the time an order's item batch lands the order + all its items are
        present and order.new renders the full item list. Idempotent + best-effort."""
        from django.conf import settings
        if getattr(settings, 'EDITION', '') != 'server':
            return
        try:
            from base.models import Order
            from notifications.handlers.order import OrderNotification
            orders = Order.objects.filter(id__in=order_ids).select_related('cashier')
            for order in orders:
                OrderNotification.dispatch(order)
        except Exception:
            logger.warning('post-receive order notify failed', exc_info=True)

    @classmethod
    def _create_or_update(cls, model_class, data, branch_id):
        data = data.copy()

        uuid_val = data.pop('uuid', None)
        if not uuid_val:
            raise ValueError('Record missing UUID')

        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        # is_deleted is popped out of `data`, so _strip_denied never sees it.
        # Without this gate a branch token could push is_deleted=True for a model
        # that lists it in SYNC_DENY_FROM_BRANCH (e.g. User) and soft-delete cloud
        # users/admins — exactly the from-branch protection the denylist promises.
        # Gate only the UPDATE paths; CREATE still honours it (matches _strip_denied's
        # create-time exception, and a brand-new tombstone is harmless).
        _del_denied = ('is_deleted' in model_class._effective_denylist()) \
            if hasattr(model_class, '_effective_denylist') else False
        # Ignore any branch_id in the payload — the receive endpoint binds
        # the auth token to one branch (BRANCH_TOKEN_MAP), so honoring a
        # per-record branch_id would let a branch-token holder write records
        # claiming any other branch's ID. Pull-from-cloud is the only path
        # where the payload branch_id is trusted (cloud is multi-tenant).
        payload_branch = data.pop('branch_id', None)
        if payload_branch and payload_branch != branch_id:
            logger.warning(
                'sync receive: dropping spoofed branch_id=%s (auth=%s) on %s',
                payload_branch, branch_id, model_class.__name__,
            )
        incoming_branch = branch_id

        resolved_fks, missing_fks = _resolve_foreign_keys(data)

        # If any *non-nullable* FK couldn't be resolved (the related model's
        # UUID hasn't synced yet), refuse to materialize the row. The
        # previous behavior was to silently persist with the FK as NULL,
        # permanently losing the association even when the parent later
        # arrived — or DB-rejecting with a NOT NULL violation. Surface as
        # an error so the caller's retry path can re-deliver after the
        # parent batch lands. Nullable FKs fall through to NULL, which is
        # what the model's `null=True` already permits.
        for uuid_field, uuid_value in missing_fks:
            fk_field_name = FK_UUID_MAPPINGS[uuid_field][2]
            try:
                fk_field = model_class._meta.get_field(fk_field_name)
            except Exception as exc:  # noqa: BLE001
                # Field lookup failure (mapping points to a field that no longer
                # exists). Log and move on so a stale FK_UUID_MAPPINGS entry can't
                # blow up the whole receive loop.
                logger.warning(
                    'sync receive: FK field %s missing on %s: %s',
                    fk_field_name, model_class.__name__, exc,
                )
                continue
            if not fk_field.null:
                if is_deleted:
                    # A tombstone whose required parent never arrived (e.g. the
                    # parent shift was deleted) is a no-op — there's nothing to
                    # delete. Skip it instead of deferring forever and flooding
                    # the queue with "parent not synced" errors.
                    logger.info(
                        'sync receive: skipping tombstone for %s; required FK '
                        '%s=%s absent', model_class.__name__, fk_field_name, uuid_value,
                    )
                    return None, 'skipped'
                raise ValueError(
                    f'Unresolved required FK on {model_class.__name__}: '
                    f'{fk_field_name}={uuid_value}. Parent record has not '
                    'synced yet — retry after the parent batch lands.'
                )

        for uuid_field in FK_UUID_MAPPINGS:
            data.pop(uuid_field, None)

        cleaned = _prepare_fields(model_class, data)

        # Per-record atomic + row lock. Without this the get → _should_replace →
        # save sequence is a read-modify-write with no isolation: two concurrent
        # receives of the same UUID both pass _should_replace against the *old*
        # version and the later writer clobbers the earlier one, defeating the
        # deterministic tiebreaker. The caller loops per record and catches
        # exceptions, so each record owns its own transaction; a rollback here
        # leaves the row untouched and the UUID is re-queued via failed_uuids.
        with transaction.atomic():
            try:
                instance = model_class.objects.select_for_update().get(uuid=uuid_val)

                # Route through SyncMixin._should_replace so the deterministic
                # tiebreaker (updated_at then branch_id) applies on equal
                # sync_version. Without this, two branches that landed at the
                # same version silently let whichever batch arrived second win.
                if hasattr(model_class, '_should_replace'):
                    if not model_class._should_replace(
                        instance, sync_version, cleaned, incoming_branch,
                    ):
                        return instance, 'skipped'
                elif sync_version < instance.sync_version:
                    return instance, 'skipped'

                # A locally-tombstoned row is terminal: never let a stale
                # incoming record that won the version/tiebreaker resurrect it
                # by clearing is_deleted (FS7). Deletes only propagate forward.
                if instance.is_deleted and not is_deleted:
                    return instance, 'skipped'

                # Preserve source-of-truth updated_at across save(): every SyncMixin
                # model declares updated_at with auto_now=True, so save() would stamp
                # the receiver's local clock and defeat the _should_replace tiebreaker
                # on every subsequent compare. Pop it and re-apply via .update(),
                # which bypasses auto_now (same approach as SyncMixin.from_sync_dict).
                incoming_updated = cleaned.pop('updated_at', None)

                for key, value in _strip_denied(model_class, cleaned, creating=False).items():
                    setattr(instance, key, value)

                for fk_field, fk_instance in resolved_fks.items():
                    setattr(instance, fk_field, fk_instance)

                instance.sync_version = sync_version
                if not _del_denied:           # SYNC_DENY_FROM_BRANCH guard (e.g. User.is_deleted)
                    instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                # Preserve the record's OWNER on update. Overwriting branch_id
                # with the pushing branch stole ownership of a cloud-owned record
                # (a branch editing a cloud-created user re-tagged it 'branch1'),
                # after which /changes excluded it from that branch's pull feed
                # and cloud edits stopped flowing down. Only tag an untagged row.
                if not instance.branch_id:
                    instance.branch_id = incoming_branch
                instance.save(_syncing=True)
                _preserve_updated_at(model_class, instance, incoming_updated)
                _preserve_created_at(model_class, instance, cleaned.get('created_at'))
                return instance, 'updated'

            except model_class.DoesNotExist:
                incoming_updated = cleaned.pop('updated_at', None)

                # Reconcile onto an existing row that already owns this model's
                # natural key (e.g. User.email) instead of INSERTing a duplicate
                # that trips the unique constraint and gets dropped + re-queued
                # forever. Converge on the incoming uuid.
                natural = None
                if hasattr(model_class, '_find_by_natural_key'):
                    natural = model_class._find_by_natural_key(
                        cleaned, resolved_fks, incoming_branch=incoming_branch,
                    )
                if natural is not None:
                    # Re-fetch under a row lock so two concurrent receives that
                    # both reconcile onto the same natural-key row serialize
                    # instead of clobbering each other.
                    instance = model_class.objects.select_for_update().get(pk=natural.pk)
                    instance.uuid = uuid_val
                    # Reconcile = UPDATE of an existing row: protect denied fields.
                    for key, value in _strip_denied(model_class, cleaned, creating=False).items():
                        setattr(instance, key, value)
                    for fk_field, fk_instance in resolved_fks.items():
                        setattr(instance, fk_field, fk_instance)
                    instance.sync_version = sync_version
                    if not _del_denied:       # SYNC_DENY_FROM_BRANCH guard (e.g. User.is_deleted)
                        instance.is_deleted = is_deleted
                    instance.synced_at = timezone.now()
                    # Reconcile = update of an existing row: preserve its owner
                    # (see the update branch above). Only tag if untagged.
                    if not instance.branch_id:
                        instance.branch_id = incoming_branch
                    instance.save(_syncing=True)
                    _preserve_updated_at(model_class, instance, incoming_updated)
                    _preserve_created_at(model_class, instance, cleaned.get('created_at'))
                    return instance, 'updated'

                instance = model_class(
                    uuid=uuid_val,
                    sync_version=sync_version,
                    is_deleted=is_deleted,
                    branch_id=incoming_branch,
                    synced_at=timezone.now(),
                )

                for key, value in _strip_denied(model_class, cleaned, creating=True).items():
                    setattr(instance, key, value)

                for fk_field, fk_instance in resolved_fks.items():
                    setattr(instance, fk_field, fk_instance)

                instance.save(_syncing=True)
                _preserve_updated_at(model_class, instance, incoming_updated)
                _preserve_created_at(model_class, instance, cleaned.get('created_at'))
                return instance, 'created'
