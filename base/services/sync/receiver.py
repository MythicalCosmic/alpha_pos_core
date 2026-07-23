import logging
from hashlib import sha256
from decimal import Decimal
from uuid import UUID
from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from base.services.sync.config import FK_UUID_MAPPINGS

logger = logging.getLogger(__name__)


class CriticalSyncConflict(ValueError):
    """A financial state transition was not authoritatively stored.

    The receive endpoint returns these UUIDs as failed so the branch's durable
    queue retains them instead of treating an HTTP 200/skipped write as proof
    that a shift close reached the cloud.
    """

    def __init__(self, record_result):
        self.record_result = record_result
        super().__init__(record_result.get('reason') or 'Critical sync conflict')


class RetryableSyncError(ValueError):
    """A valid record blocked until external state changes.

    ValueError is retained as a base class for direct/internal callers which
    historically treated an unresolved FK as a validation exception.
    receive_batch catches this subtype first and classifies it as retryable,
    never as a permanent invalid-record rejection.
    """

    def __init__(self, message, *, reason_code='RETRYABLE_APPLY_ERROR'):
        self.reason_code = reason_code
        super().__init__(message)


class SyncApplyResult:
    """Backward-compatible two-value result with an explicit disposition.

    Existing direct callers unpack ``instance, action``.  ``receive_batch`` also
    inspects ``disposition`` so a policy rejection can no longer masquerade as
    an idempotent ``skipped`` acknowledgement.
    """

    __slots__ = ('instance', 'action', 'disposition', 'reason_code', 'reason')

    def __init__(
        self, instance, action, *, disposition='acknowledged',
        reason_code='', reason='',
    ):
        self.instance = instance
        self.action = action
        self.disposition = disposition
        self.reason_code = reason_code
        self.reason = reason

    def __iter__(self):
        yield self.instance
        yield self.action


def _rejected(instance, reason_code, reason):
    return SyncApplyResult(
        instance, 'skipped', disposition='rejected',
        reason_code=reason_code, reason=reason,
    )


def _acknowledged(instance, reason_code, reason=''):
    return SyncApplyResult(
        instance, 'skipped', disposition='acknowledged',
        reason_code=reason_code, reason=reason,
    )


def _global_user_alias_key(incoming_branch, source_uuid):
    """Durable, bounded key for a legacy branch User UUID alias."""
    identity = f'{incoming_branch}:{source_uuid}'.encode('utf-8')
    digest = sha256(identity).hexdigest()[:40]
    return f'sync_user_alias:{digest}'


def _store_global_user_alias(incoming_branch, source_uuid, canonical_uuid):
    """Remember a validated email-identity alias for legacy clients.

    Pre-v2 clients cannot consume ``canonical_uuid`` response evidence.  The
    server therefore keeps the old UUID resolvable for their later Order/Shift
    FK payloads while upgraded clients re-key their local graph immediately.
    """
    from base.models import SyncState

    source = str(source_uuid)
    canonical = str(canonical_uuid)
    if not source or not canonical or source == canonical:
        return
    SyncState.objects.update_or_create(
        key=_global_user_alias_key(incoming_branch, source),
        defaults={'value': canonical},
    )


def _resolve_global_user_alias(
    related_model, incoming_branch, source_uuid,
):
    """Resolve a previously validated User UUID alias, if still canonical."""
    if (
        getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud'
        or related_model._meta.label_lower != 'base.user'
    ):
        return None
    from base.models import SyncState

    marker = SyncState.objects.filter(
        key=_global_user_alias_key(incoming_branch, source_uuid),
    ).first()
    if marker is None or not marker.value:
        return None
    try:
        canonical_uuid = UUID(str(marker.value))
    except (TypeError, ValueError, AttributeError):
        return None
    return related_model._base_manager.filter(
        uuid=canonical_uuid,
        is_deleted=False,
    ).first()


def _append_only_replay_matches(model_class, instance, cleaned, resolved_fks):
    """Prove that an append-only UUID replay carries identical evidence."""
    for field_name, incoming_value in _strip_denied(
        model_class, cleaned, creating=True,
    ).items():
        if getattr(instance, field_name) != incoming_value:
            return False
    for field_name, related in resolved_fks.items():
        incoming_pk = related.pk if related is not None else None
        if getattr(instance, f'{field_name}_id') != incoming_pk:
            return False
    return True


def _record_replay_matches(
    model_class, instance, cleaned, resolved_fks, *, is_deleted,
):
    """Prove a losing LWW record is already represented on the receiver."""
    values = _strip_denied(model_class, cleaned, creating=False)
    values = _strip_branch_rewrites(model_class, instance, values)
    for field_name, incoming_value in values.items():
        if getattr(instance, field_name) != incoming_value:
            return False
    denied = (
        model_class._effective_denylist()
        if hasattr(model_class, '_effective_denylist')
        else frozenset()
    )
    frozen = _branch_frozen_update_fields(model_class, instance)
    for field_name, related in resolved_fks.items():
        if field_name in denied or field_name in frozen:
            continue
        incoming_pk = related.pk if related is not None else None
        if getattr(instance, f'{field_name}_id') != incoming_pk:
            return False
    delete_denied = 'is_deleted' in denied
    if (
        not delete_denied
        and 'is_deleted' not in frozen
        and instance.is_deleted != is_deleted
    ):
        return False
    return True


def _resolve_foreign_keys(model_class, data, incoming_branch):
    """Resolve UUID-keyed FK references to local PKs.

    Returns (resolved, missing, forbidden) where:
      resolved: {fk_field: instance} for FKs successfully looked up
      missing:  [(uuid_field, uuid_value)] for FKs that referenced an unknown
                UUID. Any supplied-but-unknown parent is deferred; silently
                replacing a nullable relation with NULL would lose the link
                permanently when the parent arrives later.
      forbidden: references to a known parent owned by another branch. These
                 are permanent scope violations and must be acknowledged as
                 skipped rather than retried into the dead-letter queue.
    """
    resolved = {}
    missing = []
    forbidden = []
    for uuid_field, (app_label, model_name, fk_field) in FK_UUID_MAPPINGS.items():
        # Absence means "leave the relationship untouched" on a partial
        # payload.  An explicit null is different: it means "clear this
        # nullable relationship".  The old truthiness check conflated the two,
        # so a relationship removed on one node survived forever on its peer.
        if uuid_field not in data:
            continue
        try:
            field = model_class._meta.get_field(fk_field)
        except Exception:
            # The mapping is global; most UUID keys do not belong to this
            # particular model.
            continue

        uuid_value = data[uuid_field]
        if uuid_value in (None, ''):
            if field.null:
                resolved[fk_field] = None
            else:
                # Reuse the existing missing-FK validation below so required
                # relationships fail/defer instead of reaching the database as
                # an IntegrityError (or being silently retained on update).
                missing.append((uuid_field, uuid_value))
            continue
        try:
            related_model = apps.get_model(app_label, model_name)
            instance = related_model.objects.filter(uuid=uuid_value).first()
            if instance is None:
                instance = _resolve_global_user_alias(
                    related_model, incoming_branch, uuid_value,
                )
            if instance:
                parent_scope = getattr(
                    related_model, 'SYNC_PULL_SCOPE', 'branch',
                )
                parent_branch = str(getattr(instance, 'branch_id', '') or '')
                if (
                    parent_scope == 'branch'
                    and parent_branch != str(incoming_branch or '')
                ):
                    # Never let a child authenticated as branch A attach to a
                    # branch-B parent merely because it knows that row's UUID.
                    # Blank legacy ownership also requires deterministic repair,
                    # not first-writer-wins adoption during a request.
                    logger.warning(
                        'FK owner mismatch: %s uuid=%s owner=%s incoming=%s',
                        model_name, uuid_value, parent_branch, incoming_branch,
                    )
                    forbidden.append((
                        uuid_field, uuid_value, parent_branch,
                    ))
                else:
                    resolved[fk_field] = instance
            else:
                logger.warning(f'FK not found: {model_name} uuid={uuid_value}')
                missing.append((uuid_field, uuid_value))
        except Exception as e:
            logger.error(f'FK resolve error {uuid_field}: {e}')
            missing.append((uuid_field, uuid_value))
    return resolved, missing, forbidden


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
        if isinstance(value, bool):
            return value
        # JSON booleans are the canonical wire form.  Accept the two legacy
        # string spellings explicitly; Python's bool("false") is True and was
        # silently flipping paid/deleted/command state during old-client sync.
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == 'true':
                return True
            if normalized == 'false':
                return False
        # A few legacy serializers emitted JSON 0/1.  They are unambiguous,
        # unlike arbitrary non-empty strings or numbers.
        if type(value) is int and value in (0, 1):
            return bool(value)
        raise ValueError(
            'boolean values must be true/false (or legacy 0/1)',
        )

    if field_type == 'UUIDField':
        # JSON carries UUIDs as strings while Django exposes UUIDField values as
        # ``uuid.UUID`` objects.  Keeping the raw string made an exact
        # append-only payment replay look like a money-evidence rewrite.
        return field.to_python(value)

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
            if field.get_internal_type() in {'BooleanField', 'UUIDField'}:
                # Invalid financial/control booleans are permanent record
                # defects, as are malformed business UUIDs. Do not pass either
                # through to Django's looser save-time coercion and misclassify
                # them as a transient database failure.
                raise ValueError(
                    f'Invalid {field.get_internal_type()} field {key}: {e}'
                ) from e
            cleaned[key] = value

    return cleaned


def _strip_denied(model_class, cleaned, *, creating):
    # Delegate to the model's direction-aware policy when available (every
    # SyncMixin subclass has it). Non-SyncMixin models pass through unchanged.
    strip = getattr(model_class, '_strip_sync_denied', None)
    if strip is None:
        return cleaned
    return strip(cleaned, creating=creating)


def _strip_branch_rewrites(model_class, instance, values):
    """Enforce create-only and write-once branch fields on cloud updates.

    The receiver is the branch->cloud trust boundary. Model declarations alone
    are intentionally not decorative: create-only rollout identity can never
    flip later, while a non-empty close manifest may only be replayed exactly.
    Direct cloud repair remains possible through explicit server code.
    """
    values = dict(values)
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
        return values
    model_guard = getattr(model_class, '_strip_sync_branch_rewrites', None)
    if model_guard is not None:
        values = model_guard(instance, values)
    immutable_from_branch = _branch_frozen_update_fields(
        model_class, instance,
    )
    for field_name in immutable_from_branch:
        values.pop(field_name, None)
    return values


def _branch_frozen_after_manifest_fields(model_class, instance):
    """Concrete field names/attnames frozen after a branch close handshake."""
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud' or not (
        getattr(instance, 'settlement_manifest', None)
    ):
        return frozenset()
    frozen = set(getattr(
        model_class,
        'SYNC_IMMUTABLE_FROM_BRANCH_AFTER_MANIFEST',
        frozenset(),
    ))
    expanded = set(frozen)
    for name in frozen:
        try:
            field = model_class._meta.get_field(name)
        except Exception:  # noqa: BLE001 - stale declarations stay scalar-safe
            continue
        expanded.add(field.name)
        expanded.add(field.attname)
    return frozenset(expanded)


def _branch_frozen_update_fields(model_class, instance):
    """All fields/attnames a branch may no longer rewrite on the cloud."""
    frozen = set(_branch_frozen_after_manifest_fields(model_class, instance))
    settled_guard = getattr(
        model_class, '_sync_frozen_from_branch_fields', None,
    )
    if settled_guard is not None:
        frozen.update(settled_guard(instance))
    return frozenset(frozen)


def _append_only_trusted_update_fields(model_class):
    """Fields an append-only row may receive from trusted cloud on a branch."""
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud':
        return frozenset()
    return frozenset(getattr(
        model_class, 'SYNC_APPEND_ONLY_TRUSTED_UPDATE_FIELDS', frozenset(),
    ))


def _pop_automatic_values(model_class, cleaned):
    """Capture every source timestamp save() would replace with local time."""
    values = {}
    for field in model_class._meta.concrete_fields:
        if not (getattr(field, 'auto_now', False)
                or getattr(field, 'auto_now_add', False)):
            continue
        if field.name not in cleaned:
            continue
        value = cleaned.pop(field.name)
        if value is not None:
            values[field.name] = value
    return values


def _preserve_automatic_values(model_class, instance, values, *, creating):
    # QuerySet.update() bypasses auto_now/auto_now_add. This preserves both the
    # conventional fields and named event clocks such as Inkassa.period_end and
    # DiscountUsage.used_at.
    allowed = _strip_denied(model_class, values, creating=creating)
    if not allowed:
        return
    model_class.objects.filter(pk=instance.pk).update(**allowed)
    for field_name, value in allowed.items():
        setattr(instance, field_name, value)


class CloudReceiver:

    @classmethod
    def receive_batch(
        cls, model_name, branch_id, records, *, client_ack_protocol=2,
    ):
        result = {
            'ack_protocol_version': 2,
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
            'acknowledged_uuids': [],
            'retryable_uuids': [],
            'rejected_uuids': [],
            # Additive per-record evidence for state transitions where
            # created/updated/skipped is too weak to be an acknowledgement.
            'record_results': [],
        }

        if not isinstance(records, list) or not records:
            result['success'] = False
            result['errors'].append('records must be a non-empty array')
            return result
        submitted_uuids = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                result['success'] = False
                result['errors'].append(f'records[{index}] must be an object')
                return result
            record_uuid = str(record.get('uuid') or '')
            if not record_uuid:
                result['success'] = False
                result['errors'].append(f'records[{index}] is missing uuid')
                return result
            submitted_uuids.append(record_uuid)
        if len(set(submitted_uuids)) != len(submitted_uuids):
            result['success'] = False
            result['errors'].append('record UUIDs must be unique within a batch')
            return result

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
            result['success'] = False
            result['errors'].append(str(e))
            result['retryable_uuids'] = submitted_uuids
            result['failed_uuids'] = submitted_uuids
            return result

        # Per-model opt-out for state that must never arrive from a peer (for
        # example the branch-local treasury ledger). One-way collectors such
        # as AuditLog use `_sync_pull_disabled` instead: cloud receive is
        # allowed, but the model is omitted from branch change feeds.
        if getattr(model_class, '_sync_ingest_disabled', False):
            logger.info(
                'sync receive: ingest disabled for %s — skipping %d record(s)',
                model_class.__name__, len(records),
            )
            result['skipped'] = len(records)
            result['acknowledged_uuids'] = submitted_uuids
            result['record_results'] = [
                {
                    'uuid': record_uuid,
                    'action': 'skipped',
                    'disposition': 'acknowledged',
                    'reason_code': 'INGEST_DISABLED',
                }
                for record_uuid in submitted_uuids
            ]
            return result

        model_label = model_class.__name__
        affected_order_ids = set()
        affected_record_uuids = set()
        for record_data, rec_uuid in zip(records, submitted_uuids):
            try:
                if model_class._meta.label_lower == 'base.user':
                    apply_result = cls._create_or_update(
                        model_class,
                        record_data,
                        branch_id,
                        client_ack_protocol=client_ack_protocol,
                    )
                else:
                    apply_result = cls._create_or_update(
                        model_class, record_data, branch_id,
                    )
                if isinstance(apply_result, SyncApplyResult):
                    instance = apply_result.instance
                    action = apply_result.action
                    disposition = apply_result.disposition
                    reason_code = apply_result.reason_code
                    reason = apply_result.reason
                else:
                    instance, action = apply_result
                    if action in {'created', 'updated'}:
                        disposition = 'acknowledged'
                        reason_code = ''
                        reason = ''
                    else:
                        disposition = 'rejected'
                        reason_code = 'UNCLASSIFIED_SKIP'
                        reason = (
                            'Receiver could not prove that the skipped record '
                            'is semantically equivalent'
                        )
                if action == 'created':
                    result['created'] += 1
                elif action == 'updated':
                    result['updated'] += 1
                else:
                    result['skipped'] += 1
                custom_result = getattr(instance, '_sync_record_result', None)
                record_result = dict(custom_result or {})
                record_result.update({
                    'uuid': rec_uuid,
                    'action': action,
                    'disposition': disposition,
                })
                if instance is not None:
                    record_result['server_sync_version'] = getattr(
                        instance, 'sync_version', None,
                    )
                    record_result['server_is_deleted'] = getattr(
                        instance, 'is_deleted', None,
                    )
                if reason_code:
                    record_result['reason_code'] = reason_code
                if reason:
                    record_result['reason'] = reason
                    # Rolling v1 clients interpret any top-level error without
                    # failed_uuids as a whole-batch failure. Informational
                    # idempotent/alias ACK reasons belong only in per-record
                    # evidence; top-level errors are reserved for retryable or
                    # rejected records.
                    if disposition != 'acknowledged':
                        result['errors'].append(f'{rec_uuid}: {reason}')
                result['record_results'].append(record_result)
                result[f'{disposition}_uuids'].append(rec_uuid)
                # Collect the orders touched by this batch so staff notifications
                # fire AFTER the order + its items are all applied (items arrive
                # in a separate batch after the order — see _notify_received_orders).
                if instance is not None and disposition == 'acknowledged':
                    if model_label == 'Order':
                        affected_order_ids.add(instance.id)
                        affected_record_uuids.add(rec_uuid)
                    elif model_label == 'OrderItem' and instance.order_id:
                        affected_order_ids.add(instance.order_id)
                        affected_record_uuids.add(rec_uuid)
                    elif model_label in {
                        'OrderPayment', 'ExternalOrderPayment',
                    } and instance.order_id:
                        affected_order_ids.add(instance.order_id)
                        affected_record_uuids.add(rec_uuid)
            except CriticalSyncConflict as e:
                record_result = dict(e.record_result)
                record_result.update({
                    'uuid': rec_uuid,
                    'action': 'conflict',
                    'disposition': 'rejected',
                })
                result['record_results'].append(record_result)
                result['skipped'] += 1
                error_msg = f'{rec_uuid or "?"}: {e}'
                result['errors'].append(error_msg)
                result['rejected_uuids'].append(rec_uuid)
                logger.warning('Receive critical conflict: %s', error_msg)
            except RetryableSyncError as e:
                error_msg = f'{rec_uuid or "?"}: {str(e)}'
                result['errors'].append(error_msg)
                result['retryable_uuids'].append(rec_uuid)
                result['record_results'].append({
                    'uuid': rec_uuid,
                    'action': 'deferred',
                    'disposition': 'retryable',
                    'reason_code': e.reason_code,
                    'reason': str(e),
                })
                logger.info('Receive deferred: %s', error_msg)
            except ValueError as e:
                error_msg = f'{rec_uuid or "?"}: {str(e)}'
                result['errors'].append(error_msg)
                result['rejected_uuids'].append(rec_uuid)
                result['record_results'].append({
                    'uuid': rec_uuid,
                    'action': 'rejected',
                    'disposition': 'rejected',
                    'reason_code': 'INVALID_RECORD',
                    'reason': str(e),
                })
                logger.warning('Receive rejected: %s', error_msg)
            except Exception as e:
                error_msg = f'{rec_uuid or "?"}: {str(e)}'
                result['errors'].append(error_msg)
                result['retryable_uuids'].append(rec_uuid)
                result['record_results'].append({
                    'uuid': rec_uuid,
                    'action': 'deferred',
                    'disposition': 'retryable',
                    'reason_code': 'APPLY_ERROR',
                    'reason': str(e),
                })
                logger.error('Receive error: %s', error_msg, exc_info=True)

        if affected_order_ids:
            money_reconciled = True
            try:
                cls._reconcile_received_order_money(affected_order_ids)
            except Exception as exc:
                money_reconciled = False
                logger.error(
                    'post-receive money reconciliation failed', exc_info=True,
                )
                for record_uuid in sorted(affected_record_uuids):
                    if record_uuid not in result['acknowledged_uuids']:
                        continue
                    result['acknowledged_uuids'].remove(record_uuid)
                    result['retryable_uuids'].append(record_uuid)
                    for evidence in result['record_results']:
                        if evidence.get('uuid') == record_uuid:
                            evidence.update({
                                'action': 'deferred',
                                'disposition': 'retryable',
                                'reason_code': (
                                    'POST_RECEIVE_RECONCILIATION_FAILED'
                                ),
                                'reason': str(exc),
                            })
                    result['errors'].append(
                        f'{record_uuid}: post-receive reconciliation failed: '
                        f'{exc}'
                    )
            if money_reconciled:
                cls._notify_received_orders(affected_order_ids)

        try:
            cls._run_periodic_money_reconciliation()
        except Exception:
            # The touched-record path above is part of that record's ACK. This
            # bounded legacy sweep is independent and retries on the next batch.
            logger.warning(
                'periodic money reconciliation failed', exc_info=True,
            )

        for key in (
            'acknowledged_uuids', 'retryable_uuids', 'rejected_uuids',
        ):
            result[key] = list(dict.fromkeys(result[key]))
        result['failed_uuids'] = list(dict.fromkeys([
            *result['retryable_uuids'],
            *result['rejected_uuids'],
        ]))
        return result

    @staticmethod
    def _shift_close_result(
        *, uuid_val, state, instance=None, manifest=None,
        reason_code=None, reason=None,
    ):
        from core.shifts.service import settlement_manifest_digest

        manifest = manifest or {}
        return {
            'uuid': str(uuid_val),
            'kind': 'SHIFT_CLOSE',
            'state': state,
            'server_status': getattr(instance, 'status', None),
            'server_sync_version': getattr(instance, 'sync_version', None),
            'manifest_version': manifest.get('version'),
            'manifest_digest': settlement_manifest_digest(manifest),
            'reason_code': reason_code,
            'reason': reason,
        }

    @classmethod
    def _validate_shift_close_intent(
        cls, *, model_class, uuid_val, cleaned, incoming_branch,
    ):
        """Validate the immutable minimum needed to store a close header."""
        if (
            getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud'
            or model_class._meta.label_lower != 'base.shift'
            or str(cleaned.get('status') or '').upper() != 'ENDED'
        ):
            return None

        manifest = cleaned.get('settlement_manifest')
        result_kwargs = {
            'uuid_val': uuid_val,
            'state': 'CONFLICT',
            'manifest': manifest if isinstance(manifest, dict) else None,
        }
        if not isinstance(manifest, dict) or not manifest:
            raise CriticalSyncConflict(cls._shift_close_result(
                **result_kwargs,
                reason_code='MANIFEST_REQUIRED',
                reason='A shift close must include its immutable settlement manifest',
            ))
        if (
            manifest.get('version') not in {2, 3}
            or manifest.get('branch_id') != incoming_branch
            or not isinstance(manifest.get('tenders'), list)
        ):
            raise CriticalSyncConflict(cls._shift_close_result(
                **result_kwargs,
                reason_code='INVALID_CLOSE_MANIFEST',
                reason='The shift close manifest is malformed or belongs to another branch',
            ))
        end_time = cleaned.get('end_time')
        if end_time is None:
            raise CriticalSyncConflict(cls._shift_close_result(
                **result_kwargs,
                reason_code='INVALID_CLOSE_WINDOW',
                reason='A shift close must include end_time',
            ))
        if (
            not isinstance(cleaned.get('total_orders'), int)
            or cleaned['total_orders'] < 0
        ):
            raise CriticalSyncConflict(cls._shift_close_result(
                **result_kwargs,
                reason_code='INVALID_CLOSE_TOTALS',
                reason='A shift close must include frozen order and money totals',
            ))
        for field_name in ('total_revenue', 'cash_collected'):
            value = cleaned.get(field_name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CriticalSyncConflict(cls._shift_close_result(
                    **result_kwargs,
                    reason_code='INVALID_CLOSE_TOTALS',
                    reason=f'A shift close has invalid {field_name}',
                ))
        return manifest

    @staticmethod
    def _reconcile_received_order_money(order_ids):
        """Cloud-side backstop for old tills affected by the queue ACK race."""
        from django.conf import settings
        if getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud':
            return
        from base.services.order_payment_reconciliation import (
            reconcile_stale_paid_headers,
        )
        repaired = reconcile_stale_paid_headers(order_ids)
        if repaired:
            logger.warning(
                'sync receive repaired %d stale paid order header(s): %s',
                len(repaired), ','.join(sorted(repaired)),
            )

    @staticmethod
    def _run_periodic_money_reconciliation():
        """Bounded cloud sweep for rows lost by pre-v2 acknowledgement races."""
        if getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud':
            return []
        import json
        from datetime import timedelta
        from django.utils.dateparse import parse_datetime
        from base.models import (
            ExternalOrderPayment, OrderPayment, SyncState,
        )
        from base.services.order_payment_reconciliation import (
            reconcile_stale_paid_headers,
        )

        interval = max(30, int(getattr(
            settings, 'SYNC_MONEY_RECONCILE_INTERVAL_SECONDS', 300,
        )))
        limit = max(1, min(2000, int(getattr(
            settings, 'SYNC_MONEY_RECONCILE_BATCH_SIZE', 500,
        ))))
        now = timezone.now()
        with transaction.atomic():
            marker, _ = SyncState.objects.select_for_update().get_or_create(
                key='sync_money_reconcile_v2',
                defaults={'value': ''},
            )
            try:
                state = json.loads(marker.value or '{}')
                last_finished = parse_datetime(
                    state.get('last_finished_at', ''),
                )
                if last_finished is None:
                    raise ValueError('missing reconciliation timestamp')
                if timezone.is_naive(last_finished):
                    last_finished = timezone.make_aware(last_finished)
            except (TypeError, ValueError):
                last_finished = None
            if (
                last_finished is not None
                and last_finished > now - timedelta(seconds=interval)
            ):
                return []
            order_ids = list(
                OrderPayment.objects.filter(
                    is_deleted=False,
                    order__is_paid=False,
                    order__is_deleted=False,
                ).values_list('order_id', flat=True).distinct()[:limit]
            )
            remaining = limit - len(order_ids)
            if remaining:
                order_ids.extend(
                    ExternalOrderPayment.objects.filter(
                        is_deleted=False,
                        order__is_paid=False,
                        order__is_deleted=False,
                    ).exclude(
                        order_id__in=order_ids,
                    ).values_list(
                        'order_id', flat=True,
                    ).distinct()[:remaining]
                )
            repaired = reconcile_stale_paid_headers(order_ids)
            marker.value = json.dumps({
                'last_finished_at': now.isoformat(),
                'candidate_count': len(order_ids),
                'repaired_count': len(repaired),
            })
            marker.save(update_fields=['value', 'updated_at'])
        if repaired:
            logger.warning(
                'periodic sync repair restored %d paid header(s)',
                len(repaired),
            )
        return repaired

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
    def _receive_global_user_identity(
        cls, model_class, data, uuid_val, incoming_branch,
        *, client_ack_protocol=2,
    ):
        """Resolve the branch's staff UUID without accepting privilege rewrites.

        Orders depend on User UUIDs.  Treating every global User push as a
        generic catalog write stranded the whole order cluster whenever a till
        had been bootstrapped before the cloud user arrived.  Existing users
        remain completely cloud-owned.  A matching email returns the canonical
        UUID so the branch can re-key its local identity.  If neither identity
        exists, create a tightly bounded, non-admin bootstrap identity once.
        """
        from django.contrib.auth.hashers import make_password
        from django.core.validators import validate_email

        existing = model_class._base_manager.filter(uuid=uuid_val).first()
        if existing is not None:
            incoming_email = str(data.get('email') or '').strip().lower()
            if (
                existing.is_deleted
                or not incoming_email
                or incoming_email != str(existing.email or '').strip().lower()
            ):
                return _rejected(
                    existing,
                    'GLOBAL_USER_IDENTITY_MISMATCH',
                    'The UUID does not match the canonical cloud user email',
                )
            existing._sync_record_result = {
                'canonical_uuid': str(existing.uuid),
                'reason_code': 'GLOBAL_USER_UUID_AVAILABLE',
            }
            return _acknowledged(
                existing,
                'GLOBAL_USER_UUID_AVAILABLE',
                'The canonical cloud identity already exists',
            )

        email = str(data.get('email') or '').strip().lower()
        try:
            validate_email(email)
        except Exception as exc:
            return _rejected(
                None,
                'INVALID_USER_IDENTITY',
                f'A valid email is required to resolve the user identity: {exc}',
            )

        with transaction.atomic():
            canonical = (
                model_class._base_manager.select_for_update()
                .filter(email__iexact=email, is_deleted=False)
                .first()
            )
            if canonical is not None:
                _store_global_user_alias(
                    incoming_branch, uuid_val, canonical.uuid,
                )
                evidence = {
                    'reason_code': 'GLOBAL_USER_CANONICAL_ALIAS',
                }
                if int(client_ack_protocol or 1) >= 2:
                    evidence['canonical_uuid'] = str(canonical.uuid)
                canonical._sync_record_result = evidence
                return _acknowledged(
                    canonical,
                    'GLOBAL_USER_CANONICAL_ALIAS',
                    'The email is already owned by a canonical cloud identity',
                )

            canonical = model_class(
                uuid=uuid_val,
                first_name=str(data.get('first_name') or 'Branch')[:25],
                last_name=str(data.get('last_name') or 'Operator')[:25],
                email=email,
                # This row is an FK-only bridge. Cloud management must
                # explicitly activate/provision credentials before login.
                password=make_password(None),
                role=model_class.RoleChoices.USER,
                status=model_class.UserStatus.SUSPENDED,
                permissions=[],
                branch_id='cloud',
                sync_version=max(1, int(data.get('sync_version') or 1)),
                is_deleted=False,
                synced_at=None,
            )
            canonical.save(_syncing=True)
            canonical._publish_synced_at_after_commit(
                using=canonical._state.db,
            )
            canonical._sync_record_result = {
                'canonical_uuid': str(canonical.uuid),
                'reason_code': 'GLOBAL_USER_SAFE_PROVISIONED',
            }
            return SyncApplyResult(canonical, 'created')

    @classmethod
    def _create_or_update(
        cls, model_class, data, branch_id, *, client_ack_protocol=2,
    ):
        data = data.copy()

        uuid_val = data.pop('uuid', None)
        if not uuid_val:
            raise ValueError('Record missing UUID')

        # Models exposed with global pull scope are cloud-owned identities and
        # reference/catalog configuration. A branch token may consume them but
        # must never create, mutate, re-parent, rename-by-natural-key, or delete
        # them on the hub. Field deny-lists alone are insufficient here because
        # UUID adoption, FK assignment, sync_version and soft-delete are handled
        # outside the scalar-field cleaning path. Refuse the whole write before
        # resolving any relationships. The push is acknowledged as skipped so a
        # compromised/outdated till cannot poison its queue forever.
        if getattr(model_class, 'SYNC_PULL_SCOPE', 'branch') == 'global':
            if model_class._meta.label_lower == 'base.user':
                return cls._receive_global_user_identity(
                    model_class,
                    data,
                    uuid_val,
                    branch_id,
                    client_ack_protocol=client_ack_protocol,
                )
            existing = model_class._base_manager.filter(uuid=uuid_val).first()
            logger.warning(
                'sync receive: refused branch=%s write to cloud-owned %s uuid=%s',
                branch_id, model_class.__name__, uuid_val,
            )
            return _rejected(
                existing,
                'GLOBAL_MODEL_WRITE_REFUSED',
                'Branch writes to cloud-owned global models are not allowed',
            )

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

        # Branch-scoped UUIDs are owned by exactly one authenticated branch.
        # Check before resolving FKs so a forged cross-branch update is
        # acknowledged as refused instead of becoming a poison retry. Repeat
        # under the row lock below to close a concurrent-create race.
        if getattr(model_class, 'SYNC_PULL_SCOPE', 'branch') == 'branch':
            existing = model_class._base_manager.filter(uuid=uuid_val).first()
            if (
                existing is not None
                and str(existing.branch_id or '') != str(incoming_branch or '')
            ):
                logger.warning(
                    'sync receive: refused branch=%s write to %s uuid=%s '
                    'owned by branch=%s',
                    incoming_branch, model_class.__name__, uuid_val,
                    existing.branch_id,
                )
                return _rejected(
                    existing,
                    'CROSS_BRANCH_OWNER',
                    'The UUID is owned by a different branch',
                )

        # Append-only evidence may be created once, never deleted by a peer.
        if is_deleted and getattr(model_class, '_sync_append_only', False):
            return _rejected(
                None,
                'APPEND_ONLY_DELETE',
                'Append-only sync evidence cannot be deleted by a peer',
            )

        resolved_fks, missing_fks, forbidden_fks = _resolve_foreign_keys(
            model_class, data, incoming_branch,
        )

        if forbidden_fks:
            logger.warning(
                'sync receive: refused %s uuid=%s branch=%s cross-branch '
                'parent reference(s): %s',
                model_class.__name__, uuid_val, incoming_branch,
                forbidden_fks,
            )
            return _rejected(
                None,
                'CROSS_BRANCH_PARENT',
                'The record references a parent owned by another branch',
            )

        # Any non-empty parent UUID that has not arrived yet must defer, even
        # when the FK column is nullable. Persisting NULL would advance the
        # queue/cursor and permanently lose the intended association. A
        # tombstone is different: an existing child can be deleted without
        # resolving its old parent; a never-seen child tombstone is a no-op.
        tombstone_target_exists = bool(
            is_deleted and model_class.objects.filter(uuid=uuid_val).exists()
        )
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
            if is_deleted:
                if tombstone_target_exists:
                    continue
                logger.info(
                    'sync receive: skipping unseen tombstone for %s; FK '
                    '%s=%s absent', model_class.__name__, fk_field_name, uuid_value,
                )
                return _acknowledged(
                    None,
                    'UNSEEN_TOMBSTONE',
                    'The deleted record has never existed on this receiver',
                )
            relation_kind = 'nullable' if fk_field.null else 'required'
            raise RetryableSyncError(
                f'Unresolved {relation_kind} FK on {model_class.__name__}: '
                f'{fk_field_name}={uuid_value}. Parent record has not '
                'synced yet — retry after the parent batch lands.',
                reason_code='MISSING_DEPENDENCY',
            )

        for uuid_field in FK_UUID_MAPPINGS:
            data.pop(uuid_field, None)

        cleaned = _prepare_fields(model_class, data)
        close_manifest = cls._validate_shift_close_intent(
            model_class=model_class,
            uuid_val=uuid_val,
            cleaned=cleaned,
            incoming_branch=incoming_branch,
        )

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
                force_shift_close = False
                prior_sync_version = instance.sync_version

                if (
                    getattr(model_class, 'SYNC_PULL_SCOPE', 'branch') == 'branch'
                    and str(instance.branch_id or '') != str(incoming_branch or '')
                ):
                    logger.warning(
                        'sync receive: refused raced branch=%s write to %s '
                        'uuid=%s owned by branch=%s',
                        incoming_branch, model_class.__name__, uuid_val,
                        instance.branch_id,
                    )
                    return _rejected(
                        instance,
                        'CROSS_BRANCH_OWNER',
                        'The UUID is owned by a different branch',
                    )

                if close_manifest:
                    def close_conflict(code, reason):
                        raise CriticalSyncConflict(cls._shift_close_result(
                            uuid_val=uuid_val,
                            state='CONFLICT',
                            instance=instance,
                            manifest=close_manifest,
                            reason_code=code,
                            reason=reason,
                        ))

                    incoming_user = resolved_fks.get('user')
                    if (
                        incoming_user is not None
                        and incoming_user.pk != instance.user_id
                    ):
                        close_conflict(
                            'CLOSE_OWNER_MISMATCH',
                            'The close owner differs from the cloud shift owner',
                        )
                    incoming_start = cleaned.get('start_time')
                    if (
                        incoming_start is not None
                        and incoming_start != instance.start_time
                    ):
                        close_conflict(
                            'CLOSE_WINDOW_MISMATCH',
                            'The close start_time differs from the cloud shift window',
                        )
                    if cleaned['end_time'] <= instance.start_time:
                        close_conflict(
                            'INVALID_CLOSE_WINDOW',
                            'The close end_time must be later than start_time',
                        )

                    stored_closed = instance.status in {
                        'ENDED', 'COMPLETED',
                    }
                    same_header = (
                        instance.end_time == cleaned['end_time']
                        and instance.total_orders == cleaned['total_orders']
                        and instance.total_revenue == cleaned['total_revenue']
                        and instance.cash_collected == cleaned['cash_collected']
                    )
                    stored_manifest = instance.settlement_manifest or {}
                    if stored_closed:
                        if not same_header:
                            close_conflict(
                                'CLOSE_TOTALS_MISMATCH',
                                'The replayed close differs from frozen cloud totals',
                            )
                        if stored_manifest == close_manifest:
                            instance._sync_record_result = cls._shift_close_result(
                                uuid_val=uuid_val,
                                state='STORED',
                                instance=instance,
                                manifest=stored_manifest,
                            )
                            return _acknowledged(
                                instance,
                                'IDEMPOTENT_SHIFT_CLOSE_REPLAY',
                                'The immutable close manifest matches exactly',
                            )
                        if stored_manifest or instance.status == 'COMPLETED':
                            close_conflict(
                                'CLOSE_MANIFEST_MISMATCH',
                                'The replayed close differs from the immutable cloud manifest',
                            )
                        # Repair a previously stored ENDED legacy header by
                        # attaching the first immutable manifest. Header values
                        # were proven identical above.
                        force_shift_close = True
                    elif (
                        instance.status == 'ACTIVE'
                        and instance.end_time is None
                    ):
                        # A close is irreversible branch-owned evidence. Apply a
                        # valid manifest even when an unrelated cloud-side save
                        # advanced sync_version and ordinary LWW would skip it.
                        force_shift_close = True
                    else:
                        close_conflict(
                            'INVALID_CLOUD_SHIFT_STATE',
                            f'The cloud shift cannot close from {instance.status}',
                        )

                trusted_append_fields = frozenset()
                if getattr(model_class, '_sync_append_only', False):
                    trusted_append_fields = _append_only_trusted_update_fields(
                        model_class,
                    )
                    if not trusted_append_fields:
                        # UUID is the immutable event identity. A replay is an
                        # idempotent no-op only when every evidence field still
                        # matches; a higher version cannot rewrite history.
                        if _append_only_replay_matches(
                            model_class, instance, cleaned, resolved_fks,
                        ):
                            return _acknowledged(
                                instance,
                                'IDEMPOTENT_APPEND_ONLY_REPLAY',
                                'Append-only evidence matches exactly',
                            )
                        return _rejected(
                            instance,
                            'APPEND_ONLY_REWRITE',
                            'The UUID already stores different append-only evidence',
                        )
                    # A branch pulling a cloud manager result may update only
                    # the explicitly declared acknowledgement field(s). The
                    # locally frozen financial evidence remains append-only.
                    cleaned = {
                        key: value for key, value in cleaned.items()
                        if key in trusted_append_fields
                    }
                    resolved_fks = {}

                # Route through SyncMixin._should_replace so the deterministic
                # tiebreaker (updated_at then branch_id) applies on equal
                # sync_version. Without this, two branches that landed at the
                # same version silently let whichever batch arrived second win.
                if hasattr(model_class, '_should_replace'):
                    if not force_shift_close and not model_class._should_replace(
                        instance, sync_version, cleaned, incoming_branch,
                    ):
                        if _record_replay_matches(
                            model_class,
                            instance,
                            cleaned,
                            resolved_fks,
                            is_deleted=is_deleted,
                        ):
                            return _acknowledged(
                                instance,
                                'IDEMPOTENT_RECORD_REPLAY',
                                'The receiver already stores the same values',
                            )
                        return _rejected(
                            instance,
                            'STALE_VERSION',
                            'The incoming record lost version conflict resolution',
                        )
                elif not force_shift_close and sync_version < instance.sync_version:
                    if _record_replay_matches(
                        model_class,
                        instance,
                        cleaned,
                        resolved_fks,
                        is_deleted=is_deleted,
                    ):
                        return _acknowledged(
                            instance,
                            'IDEMPOTENT_RECORD_REPLAY',
                            'The receiver already stores the same values',
                        )
                    return _rejected(
                        instance,
                        'STALE_VERSION',
                        'The incoming record has an older sync version',
                    )

                # A locally-tombstoned row is terminal: never let a stale
                # incoming record that won the version/tiebreaker resurrect it
                # by clearing is_deleted (FS7). Deletes only propagate forward.
                if instance.is_deleted and not is_deleted:
                    return _rejected(
                        instance,
                        'TOMBSTONE_RESURRECTION',
                        'A tombstoned record cannot be resurrected by sync',
                    )

                # Capture every automatic source timestamp before save() stamps
                # the receiver clock; restore them with QuerySet.update below.
                automatic_values = _pop_automatic_values(model_class, cleaned)

                update_values = _strip_denied(
                    model_class, cleaned, creating=False,
                )
                update_values = _strip_branch_rewrites(
                    model_class, instance, update_values,
                )
                for key, value in update_values.items():
                    setattr(instance, key, value)

                denied = model_class._effective_denylist() \
                    if hasattr(model_class, '_effective_denylist') else frozenset()
                branch_frozen = _branch_frozen_update_fields(
                    model_class, instance,
                )
                for fk_field, fk_instance in resolved_fks.items():
                    if fk_field not in denied and fk_field not in branch_frozen:
                        setattr(instance, fk_field, fk_instance)

                instance.sync_version = (
                    max(prior_sync_version, sync_version) + 1
                    if force_shift_close and sync_version <= prior_sync_version
                    else sync_version
                )
                if (
                    not _del_denied
                    and 'is_deleted' not in branch_frozen
                ):  # class denylist + settled-row guard
                    instance.is_deleted = is_deleted
                # Keep this version outside the timestamp feed until its
                # per-record transaction commits.  A NULL row is still served
                # by /changes, so a process crash before the callback can cause
                # a duplicate delivery but never a permanently skipped change.
                instance.synced_at = None
                # Preserve the record's OWNER on update. Overwriting branch_id
                # with the pushing branch stole ownership of a cloud-owned record
                # (a branch editing a cloud-created user re-tagged it 'branch1'),
                # after which /changes excluded it from that branch's pull feed
                # and cloud edits stopped flowing down. Only tag an untagged row.
                if not instance.branch_id:
                    instance.branch_id = incoming_branch
                instance.save(_syncing=True)
                _preserve_automatic_values(
                    model_class, instance, automatic_values, creating=False,
                )
                instance._publish_synced_at_after_commit(using=instance._state.db)
                if close_manifest:
                    instance._sync_record_result = cls._shift_close_result(
                        uuid_val=uuid_val,
                        state='STORED',
                        instance=instance,
                        manifest=instance.settlement_manifest,
                    )
                return instance, 'updated'

            except model_class.DoesNotExist:
                automatic_values = _pop_automatic_values(model_class, cleaned)

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
                    if getattr(model_class, '_sync_append_only', False):
                        # The natural key identifies the same immutable event
                        # under a different UUID. ACKing it would strand future
                        # child references to the sender's UUID, so surface the
                        # identity conflict and keep the outbound evidence.
                        return _rejected(
                            instance,
                            'APPEND_ONLY_IDENTITY_CONFLICT',
                            'The natural key exists under a different UUID',
                        )
                    instance.uuid = uuid_val
                    # Reconcile = UPDATE of an existing row: protect denied fields.
                    update_values = _strip_denied(
                        model_class, cleaned, creating=False,
                    )
                    update_values = _strip_branch_rewrites(
                        model_class, instance, update_values,
                    )
                    for key, value in update_values.items():
                        setattr(instance, key, value)
                    denied = model_class._effective_denylist() \
                        if hasattr(model_class, '_effective_denylist') else frozenset()
                    branch_frozen = _branch_frozen_update_fields(
                        model_class, instance,
                    )
                    for fk_field, fk_instance in resolved_fks.items():
                        if fk_field not in denied and fk_field not in branch_frozen:
                            setattr(instance, fk_field, fk_instance)
                    instance.sync_version = sync_version
                    if (
                        not _del_denied
                        and 'is_deleted' not in branch_frozen
                    ):  # class denylist + settled-row guard
                        instance.is_deleted = is_deleted
                    instance.synced_at = None
                    # Reconcile = update of an existing row: preserve its owner
                    # (see the update branch above). Only tag if untagged.
                    if not instance.branch_id:
                        instance.branch_id = incoming_branch
                    instance.save(_syncing=True)
                    _preserve_automatic_values(
                        model_class, instance, automatic_values, creating=False,
                    )
                    instance._publish_synced_at_after_commit(using=instance._state.db)
                    return instance, 'updated'

                branch_create_guard = getattr(
                    model_class, 'branch_sync_create_allowed', None,
                )
                if (
                    getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
                    and branch_create_guard is not None
                    and not branch_create_guard(
                        uuid_val=uuid_val,
                        values=cleaned,
                        resolved_fks=resolved_fks,
                    )
                ):
                    logger.warning(
                        'sync receive: refused uncommitted %s create uuid=%s',
                        model_class.__name__, uuid_val,
                    )
                    return _rejected(
                        None,
                        'CREATE_POLICY_REFUSED',
                        'The record is not eligible for branch-side creation',
                    )

                instance = model_class(
                    uuid=uuid_val,
                    sync_version=sync_version,
                    is_deleted=is_deleted,
                    branch_id=incoming_branch,
                    synced_at=None,
                )

                for key, value in _strip_denied(model_class, cleaned, creating=True).items():
                    setattr(instance, key, value)

                denied = model_class._effective_denylist() \
                    if hasattr(model_class, '_effective_denylist') else frozenset()
                for fk_field, fk_instance in resolved_fks.items():
                    if fk_field not in denied:
                        setattr(instance, fk_field, fk_instance)

                instance.save(_syncing=True)
                _preserve_automatic_values(
                    model_class, instance, automatic_values, creating=True,
                )
                instance._publish_synced_at_after_commit(using=instance._state.db)
                if close_manifest:
                    instance._sync_record_result = cls._shift_close_result(
                        uuid_val=uuid_val,
                        state='STORED',
                        instance=instance,
                        manifest=instance.settlement_manifest,
                    )
                return instance, 'created'
