import logging
from django.utils import timezone
from base.services.sync.cache import safe_add, safe_delete
from base.services.sync.config import (
    SYNC_ORDER, SyncConfig, get_branch_id, is_local_mode,
    get_all_models, get_sync_batch_size,
)
from base.services.sync.queue import SyncQueue
from base.services.sync.transport import check_health, send_batch, fetch_changes
from base.services.sync.status import SyncStatus

logger = logging.getLogger(__name__)

LOCK_TTL = 120


class _QuarantinedRecordDeferred(Exception):
    """Internal control flow: rollback a temporary quarantine restore."""


class SyncService:

    @classmethod
    def queue_record(cls, instance):
        if not SyncConfig.is_enabled():
            return

        model_name = instance.__class__.__name__.lower()
        SyncQueue.add(model_name, str(instance.uuid), instance.to_sync_dict())

    @classmethod
    def queue_tombstone(cls, model_name, uuid_val, payload):
        # Push a delete-marker payload for a record that has been hard-deleted
        # locally, so the peer applies the same deletion semantics.
        if not SyncConfig.is_enabled():
            return
        SyncQueue.add(model_name, uuid_val, payload)

    @classmethod
    def push(cls):
        if not SyncConfig.is_enabled():
            return {'success': False, 'message': 'Sync not enabled'}

        if not is_local_mode():
            return {'success': False, 'message': 'Push only available in local mode'}

        push_token = cls._acquire_lock('push')
        if not push_token:
            return {'success': False, 'message': 'Push already in progress'}

        try:
            if not check_health():
                SyncStatus.set_online(False)
                cls._notify_error('Cannot reach cloud server')
                return {'success': False, 'message': 'Cannot reach cloud server', 'offline': True}

            SyncStatus.set_online(True)

            # Self-heal rows whose transaction.on_commit enqueue silently failed
            # (DB hiccup / queue-table lock): SyncMixin.save() leaves synced_at
            # NULL and relies on on_commit to enqueue, but _queue_for_sync
            # swallows exceptions — such a row is live locally yet never pushed.
            # The queue is a cache, not the source of truth; reconcile any
            # unsynced row that isn't already queued before building the batch.
            cls._reconcile_unsynced()

            grouped = SyncQueue.get_grouped()
            if not grouped:
                return {'success': True, 'message': 'Nothing to sync', 'synced': 0}

            sorted_models = sorted(
                grouped.keys(),
                key=lambda m: SYNC_ORDER.index(m) if m in SYNC_ORDER else 999
            )

            total_synced = 0
            total_failed = 0
            errors = []
            synced_uuids = []
            # Exact queue snapshots confirmed by the receiver. Keep their
            # generation + payload version, not only UUID: a save may replace a
            # queue slot while its older HTTP request is in flight.
            confirmed_by_model = {}
            batch_size = get_sync_batch_size()

            # A full-batch send failure (transport down, HTTP error) means the
            # whole-batch send is failing, not one record. Stop the entire push:
            # continuing to push dependent models would just fail FK resolution
            # on the cloud (their parents didn't land) and stack more blocking
            # retry backoff. Everything stays queued and retries next cycle.
            stop_push = False
            for model_name in sorted_models:
                if stop_push:
                    break
                records = grouped[model_name]

                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    batch_data = [r['data'] for r in batch]
                    batch_uuids = [r['uuid'] for r in batch]
                    batch_generations = {
                        r['uuid']: r.get('generation') for r in batch
                    }

                    result = send_batch(model_name, batch_data)

                    if result['success']:
                        # A 200 can still carry per-record failures (partial
                        # batch). Remove ONLY the records the receiver confirmed
                        # and keep the rest queued — purging the whole batch on
                        # the HTTP-200 silently lost the failed rows.
                        failed = {
                            str(u) for u in (result.get('failed_uuids') or [])
                        }
                        confirmed_records = [
                            r for r in batch if r['uuid'] not in failed
                        ]
                        confirmed = [r['uuid'] for r in confirmed_records]
                        synced_uuids.extend(confirmed)
                        confirmed_by_model.setdefault(model_name, []).extend(
                            confirmed_records
                        )
                        total_synced += len(confirmed_records)
                        if failed:
                            failed_in_batch = [u for u in batch_uuids if u in failed]
                            total_failed += len(failed_in_batch)
                            SyncQueue.mark_batch_failed(
                                failed_in_batch,
                                'receiver rejected record(s) in a partial batch',
                                model_name=model_name,
                                generations=batch_generations,
                            )
                            msg = (f'{model_name}: {len(failed_in_batch)} of '
                                   f'{len(batch_uuids)} record(s) rejected by receiver')
                            errors.append(msg)
                            logger.warning(msg)
                            # A rejected parent (Order, Product, etc.) means
                            # later models may depend on a row that is still
                            # absent. Do not burn retry attempts/dead-letter
                            # children on guaranteed FK failures. Confirmed
                            # siblings are still acknowledged below; the next
                            # push retries the failed parent before dependents.
                            stop_push = True
                        logger.info(f'Synced {len(confirmed)} {model_name} records')
                        if stop_push:
                            break
                    else:
                        total_failed += len(batch)
                        error_msg = f'{model_name}: {result.get("error", "Unknown")}'
                        errors.append(error_msg)
                        SyncQueue.mark_batch_failed(
                            batch_uuids, result.get('error', 'Unknown'),
                            model_name=model_name,
                            generations=batch_generations,
                        )
                        logger.warning(f'Sync failed for {model_name}: {result.get("error")}')
                        stop_push = True
                        break

            acknowledged_by_model = {}
            if synced_uuids:
                # Remove per model and exact generation. A confirmed UUID may
                # already have a newer payload in the same queue slot; that row
                # must survive this older response.
                for mname, snapshots in confirmed_by_model.items():
                    if not snapshots:
                        continue
                    acknowledged = SyncQueue.acknowledge(snapshots, mname)
                    acknowledged_by_model[mname] = [
                        r for r in snapshots if r['uuid'] in acknowledged
                    ]

            # Stamp only a live row whose version is exactly what the cloud
            # confirmed and whose queue slot has no replacement. An edit during
            # HTTP leaves synced_at NULL and is rebuilt/sent next cycle.
            if acknowledged_by_model:
                models_map = get_all_models()
                now = timezone.now()
                from django.db import transaction
                from base.models import SyncQueueRecord
                for mname, snapshots in acknowledged_by_model.items():
                    mclass = models_map.get(mname)
                    if not mclass:
                        continue
                    for snapshot in snapshots:
                        sent_version = snapshot.get('data', {}).get('sync_version')
                        if sent_version is None:
                            continue
                        with transaction.atomic():
                            instance = (
                                mclass.objects.select_for_update()
                                .filter(
                                    uuid=snapshot['uuid'],
                                    sync_version=sent_version,
                                )
                                .first()
                            )
                            if instance is None:
                                continue
                            if SyncQueueRecord.objects.filter(
                                model_name=mname,
                                record_uuid=instance.uuid,
                            ).exists():
                                continue
                            mclass.objects.filter(pk=instance.pk).update(
                                synced_at=now,
                            )

            SyncStatus.set_last_sync(total_synced, total_failed, errors)

            if total_synced > 0 and total_failed == 0:
                cls._notify_success(total_synced)
            elif errors:
                cls._notify_error(errors[0])

            return {
                'success': total_failed == 0,
                'synced': total_synced,
                'failed': total_failed,
                'errors': errors,
            }
        finally:
            cls._release_lock('push', push_token)

    @classmethod
    def pull_from_cloud(cls):
        if not SyncConfig.is_enabled():
            return {'success': False, 'message': 'Sync not enabled'}

        if not is_local_mode():
            return {'success': False, 'message': 'Pull only available in local mode'}

        from base.services.sync.config import get_pull_enabled
        if not get_pull_enabled():
            return {'success': False, 'message': 'Pull disabled'}

        # Must happen after migrations and before reading the old cursor. It is
        # idempotent and local-only; cloud aggregate state is never cleared.
        SyncStatus.ensure_scope_epoch()

        pull_token = cls._acquire_lock('pull')
        if not pull_token:
            return {'success': False, 'message': 'Pull already in progress'}

        try:
            if not check_health():
                SyncStatus.set_online(False)
                return {'success': False, 'message': 'Cannot reach cloud server', 'offline': True}

            SyncStatus.set_online(True)

            cursor = SyncStatus.get_cursor()
            persisted_cursor = cursor

            models = get_all_models()
            total_created = 0
            total_updated = 0
            errors = []
            # Records deferred because a required FK parent hadn't been applied
            # yet, keyed by model class. Retried after paging completes so a
            # child fetched on an earlier page than its parent isn't lost.
            deferred_by_model = {}
            fully_drained = False
            final_server_ts = None
            # Page through the change set. The server caps each response at
            # per_page and returns has_more + next_since (the frontier that is
            # safe to resume from). We must follow that frontier instead of
            # jumping the cursor to server_timestamp, or every record past the
            # first page is permanently lost on a long-disconnected branch.
            MAX_PAGES = 10000  # safety bound against a misbehaving server
            for _ in range(MAX_PAGES):
                result = fetch_changes(since_timestamp=cursor)
                if not result['success']:
                    error = result.get('error', 'Unknown')
                    cls._notify_error(f'Pull failed: {error}')
                    SyncStatus.set_last_pull(
                        total_created, total_updated, [str(error)]
                    )
                    return {'success': False, 'message': error,
                            'created': total_created, 'updated': total_updated}

                data = result.get('data', {})
                for name in SYNC_ORDER:
                    if name not in data:
                        continue
                    model_class = models.get(name)
                    if not model_class:
                        continue
                    apply_result = cls._apply_records(model_class, data[name])
                    total_created += apply_result['created']
                    total_updated += apply_result['updated']
                    if apply_result['errors']:
                        errors.extend(apply_result['errors'])
                    if apply_result['deferred']:
                        deferred_by_model.setdefault(model_class, []).extend(
                            apply_result['deferred']
                        )

                has_more = result.get('has_more', False)
                next_since = result.get('next_since')
                server_ts = result.get('server_timestamp')

                if not has_more:
                    # Do not publish the terminal cursor until FK-deferred
                    # records have been retried below. Advancing now makes an
                    # unresolved child disappear forever on the next pull.
                    fully_drained = True
                    final_server_ts = server_ts
                    break

                if not next_since or next_since == cursor:
                    # Server reports more but can't give us a frontier to
                    # advance to (e.g. NULL synced_at rows). Stop without
                    # advancing the cursor so the next pull retries from the
                    # same point rather than skipping the unfetched tail.
                    logger.warning(
                        'Pull: has_more set but cursor cannot advance '
                        '(next_since=%r); stopping to avoid data loss', next_since
                    )
                    errors.append('Cloud change feed cursor did not advance')
                    break

                # Advance in memory only. A child from an earlier page may be
                # waiting for a parent on a later page. Persisting this frontier
                # before deferred records resolve would make a crash permanently
                # skip that child. Re-applying after a crash is idempotent.
                cursor = next_since
            else:
                logger.warning('Pull: hit MAX_PAGES (%s); will resume next cycle', MAX_PAGES)
                errors.append(f'Pull exceeded safety limit of {MAX_PAGES} pages')

            # Retry FK-deferred records now that the whole change set is applied
            # (a full-drain pull fetches parents across pages in dependency
            # order). A few passes resolve grandparent→parent→child chains that
            # arrived out of page order. Whatever is still unresolved is a
            # genuine orphan (parent never present) — log it rather than lose it
            # silently. Re-applies are idempotent upserts.
            MAX_FK_RETRY_PASSES = 3
            for _ in range(MAX_FK_RETRY_PASSES):
                if not deferred_by_model:
                    break
                next_deferred = {}
                progressed = False
                for model_class, recs in deferred_by_model.items():
                    retry_result = cls._apply_records(model_class, recs)
                    total_created += retry_result['created']
                    total_updated += retry_result['updated']
                    if retry_result['created'] or retry_result['updated']:
                        progressed = True
                    if retry_result['deferred']:
                        next_deferred[model_class] = retry_result['deferred']
                deferred_by_model = next_deferred
                if not progressed:
                    break
            if not deferred_by_model:
                # Any per-record exception from an earlier pass was transient
                # and has now been applied successfully. Do not leave desktop
                # health red after recovery.
                errors = [
                    error for error in errors
                    if not isinstance(error, dict)
                ]
            if deferred_by_model:
                stuck = sum(len(v) for v in deferred_by_model.values())
                logger.warning(
                    'Pull: %d record(s) unresolved after retries (missing parent?)',
                    stuck,
                )
                errors.append(f'{stuck} record(s) unresolved (missing parent)')

            # A cursor proves every record at or below it was durably applied.
            # If anything remains unresolved, retain the cursor from the start
            # of this run so the server redelivers that evidence next cycle.
            if not deferred_by_model:
                cursor_to_persist = final_server_ts if fully_drained else cursor
                if cursor_to_persist and cursor_to_persist != persisted_cursor:
                    SyncStatus.set_cursor(cursor_to_persist)

            SyncStatus.set_last_pull(total_created, total_updated, [str(e) for e in errors[:1]])

            total = total_created + total_updated
            if total > 0 and not errors:
                cls._notify_pull_success(total_created, total_updated)
            elif errors:
                cls._notify_error(f'Pull errors: {errors[0]}')

            return {
                'success': not errors and not deferred_by_model,
                'created': total_created,
                'updated': total_updated,
                'errors': [str(e) for e in errors],
            }
        finally:
            cls._release_lock('pull', pull_token)

    @classmethod
    def get_unsynced(cls, model_class, branch_id=None):
        qs = model_class.objects.unsynced()
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        return [obj.to_sync_dict() for obj in qs]

    @classmethod
    def get_status(cls):
        pending, failed = SyncQueue.count()
        status_data = SyncStatus.get()

        return {
            'enabled': SyncConfig.is_enabled(),
            'mode': get_branch_id(),
            'is_online': status_data.get('is_online', False),
            'last_sync': status_data.get('last_sync'),
            'last_pull': SyncStatus.get_cursor(),
            'pending_count': pending,
            'failed_count': failed,
            'dead_letter_count': SyncQueue.dead_letter_count(),
            'last_error': status_data.get('last_error'),
            'pending_by_model': SyncQueue.get_summary(),
        }

    @classmethod
    def full_push(cls):
        if not SyncConfig.is_enabled():
            return {'success': False, 'message': 'Sync not enabled'}

        if not is_local_mode():
            return {'success': False, 'message': 'Push only available in local mode'}

        branch = get_branch_id()
        models = get_all_models()

        for name in SYNC_ORDER:
            model_class = models.get(name)
            if not model_class:
                continue

            qs = model_class.objects.all()
            if branch:
                qs = qs.filter(branch_id=branch)

            for obj in qs.iterator():
                SyncQueue.add(name, str(obj.uuid), obj.to_sync_dict())

        return cls.push()

    @classmethod
    def status_report(cls):
        branch = get_branch_id()
        models = get_all_models()
        models_status = {}

        for name in SYNC_ORDER:
            model_class = models.get(name)
            if not model_class:
                continue

            qs = model_class.objects.all()
            if branch:
                qs = qs.filter(branch_id=branch)

            # One aggregate instead of three full scans. The `last_synced`
            # lookup still needs its own query (a Max() aggregate over the
            # same row set), so we keep that as the cheaper of the two.
            from django.db.models import Count, Q, Max
            counts = qs.aggregate(
                total=Count('id'),
                synced=Count('id', filter=Q(synced_at__isnull=False)),
                unsynced=Count('id', filter=Q(synced_at__isnull=True)),
                last_synced=Max('synced_at'),
            )
            total = counts['total']
            synced = counts['synced']
            unsynced = counts['unsynced']
            last_synced = counts['last_synced']

            models_status[name] = {
                'total': total,
                'synced': synced,
                'unsynced': unsynced,
                'last_synced': last_synced.isoformat() if last_synced else None,
            }

        status_data = SyncStatus.get()
        return {
            'success': True,
            'branch_id': branch,
            'last_push': status_data.get('last_sync'),
            'last_pull': SyncStatus.get_cursor(),
            'models': models_status,
        }

    @classmethod
    def _reconcile_unsynced(cls):
        """Make every unsynced row's latest payload present in the queue.

        Backstop for the on_commit enqueue path: if _queue_for_sync ever fails
        (and it swallows the exception), the row is saved with synced_at=NULL
        but never enqueued. It also refreshes a pre-existing slot whose payload
        became stale while SYNC_ON_SAVE was disabled. SyncQueue.add preserves an
        identical slot's retry state, so this cannot accidentally revive poison
        content every cycle. Bounded work after confirmed rows are stamped.
        """
        branch = get_branch_id()
        models = get_all_models()
        requeued = 0
        for name in SYNC_ORDER:
            model_class = models.get(name)
            if not model_class:
                continue
            try:
                qs = model_class.objects.unsynced()
                if branch:
                    qs = qs.filter(branch_id=branch)
                for obj in qs.iterator():
                    # add() compares content with any existing slot. Identical
                    # payloads retain their generation/retry count; newer edits
                    # rotate generation and revive a corrected dead letter.
                    SyncQueue.add(name, str(obj.uuid), obj.to_sync_dict())
                    requeued += 1
            except Exception as e:
                logger.warning('reconcile unsynced failed for %s: %s', name, e)
        if requeued:
            logger.info('Sync reconcile: refreshed %d unsynced record(s)', requeued)
        return requeued

    @classmethod
    def _apply_records(cls, model_class, records, source_branch=None):
        # `deferred` holds records that couldn't be applied yet because a
        # required FK parent isn't present (or a transient error hit). The pull
        # loop retries them once the rest of the change set has landed, so a
        # child pulled before its parent isn't lost when the cursor advances.
        from django.conf import settings
        from django.db import transaction
        results = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': [], 'deferred': []}
        affected_order_ids = set()
        model_label = model_class.__name__
        for record in records:
            try:
                if (
                    getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'local'
                    and getattr(model_class, 'SYNC_PULL_SCOPE', 'branch') == 'branch'
                ):
                    own_branch = str(getattr(settings, 'BRANCH_ID', '') or '')
                    record_branch = str(
                        record.get('branch_id') or source_branch or ''
                    )
                    if not own_branch or record_branch != own_branch:
                        logger.warning(
                            'Pull: refused %s uuid=%s targeted to branch=%s '
                            'on local branch=%s',
                            model_class.__name__, record.get('uuid'),
                            record_branch, own_branch,
                        )
                        results['skipped'] += 1
                        continue
                # Each record applies in its own transaction so the get()->
                # compare->save inside from_sync_dict (which now takes a
                # select_for_update row lock) is atomic against a concurrent
                # push receiver applying the same uuid. Without this the pull
                # path could read a row, then a concurrent writer commits, and
                # the pull's stale save() clobbers the newer version.
                with transaction.atomic():
                    quarantine_marker = SyncStatus.restore_quarantined_target(
                        model_class, record,
                    )
                    instance, action = model_class.from_sync_dict(
                        record, branch_id=source_branch,
                    )
                    if action == 'deferred' and quarantine_marker:
                        # Roll back the temporary restore and all partial work;
                        # the durable marker remains for the retry.
                        raise _QuarantinedRecordDeferred()
                    SyncStatus.finish_quarantine_restore(quarantine_marker)
                if action == 'deferred':
                    results['deferred'].append(record)
                elif action in ('created', 'updated', 'skipped'):
                    results[action] += 1
                # Order payment headers are deliberately protected from a raw
                # cloud overwrite on a till.  Re-derive them from concrete
                # pulled tender evidence instead. Track Order/Item too so a
                # payment that arrived on an earlier feed page is retried when
                # its full parent/item evidence lands later.
                if instance is not None:
                    if model_label == 'Order':
                        affected_order_ids.add(instance.id)
                    elif model_label in (
                        'OrderItem', 'OrderPayment', 'ExternalOrderPayment',
                    ) and getattr(
                        instance, 'order_id', None,
                    ):
                        affected_order_ids.add(instance.order_id)
            except _QuarantinedRecordDeferred:
                results['deferred'].append(record)
            except Exception as e:
                results['errors'].append({
                    'uuid': record.get('uuid'),
                    'error': str(e),
                })
                results['deferred'].append(record)
                results['skipped'] += 1

        if affected_order_ids and getattr(settings, 'DEPLOYMENT_MODE', '') == 'local':
            try:
                from base.services.order_payment_reconciliation import (
                    reconcile_stale_paid_headers,
                )
                # These are branch-targeted records from the authenticated
                # cloud feed. Full live tender coverage is itself immutable
                # payment evidence. A later local READY/status edit may have
                # cleared synced_at or advanced updated_at, but it is not an
                # unpay event and must not leave the same bill collectable.
                repaired = reconcile_stale_paid_headers(
                    affected_order_ids,
                    require_later_sync_evidence=False,
                )
                if repaired:
                    logger.warning(
                        'Pull repaired %d evidence-backed paid order header(s): %s',
                        len(repaired), ','.join(sorted(repaired)),
                    )
            except Exception as exc:  # noqa: BLE001
                # Do not advance the change-feed cursor past a transient
                # reconciliation failure. Exact record replays are idempotent
                # and will drive this hook again on the deferred pass/next pull.
                logger.warning(
                    'post-pull money reconciliation failed for %s',
                    model_label,
                    exc_info=True,
                )
                results['errors'].append({
                    'uuid': None,
                    'error': f'post-pull money reconciliation failed: {exc}',
                })
                results['deferred'].extend(records)
        return results

    @classmethod
    def _acquire_lock(cls, name):
        # Store a per-acquisition owner token (not a bare True) and return it so
        # _release_lock only deletes the lock THIS caller holds. Without a token,
        # a caller whose lock expired mid-run (LOCK_TTL elapsed) and was
        # re-acquired by a second worker would delete the second worker's lock on
        # its own finally — silently allowing two concurrent push/pull runs.
        import uuid
        token = uuid.uuid4().hex
        if safe_add(f'sync:lock:{name}', token, LOCK_TTL):
            return token
        return None

    @classmethod
    def _release_lock(cls, name, token=None):
        from base.services.sync.cache import safe_get
        key = f'sync:lock:{name}'
        # Only release if we still own it. If our token no longer matches, the
        # lock expired and another worker holds it now — leave theirs intact.
        if token is not None and safe_get(key) != token:
            return
        safe_delete(key)

    @classmethod
    def _sync_recipients(cls):
        """Telegram chats that should receive the SYNC messages. Sync push/pull/
        error notices ride the 'system' category, so a chat muted from 'system'
        in the desktop panel's per-chat routing stops getting them. Empty list ⇒
        nobody is subscribed, so the caller skips the send."""
        from notifications.models import NotificationSettings
        return NotificationSettings.load().recipients_for('system')

    @classmethod
    def _notify_success(cls, count):
        try:
            from base.notifications.config import NotificationConfig
            if not NotificationConfig.is_enabled():
                return
            recipients = cls._sync_recipients()
            if not recipients:
                return
            from base.notifications.telegram import TelegramAPI
            from base.notifications.helpers import format_datetime
            _, time_str = format_datetime()
            text = (
                f'<b>SYNC MUVAFFAQIYATLI</b>\n\n'
                f'Yuborildi: <b>{count}</b> ta yozuv\n'
                f'Branch: {get_branch_id()}\n'
                f'Vaqt: {time_str}'
            )
            TelegramAPI.send_message(text, chat_ids=recipients)
        except Exception as e:
            logger.debug(f'Sync notification skipped: {e}')

    @classmethod
    def _notify_pull_success(cls, created, updated):
        try:
            from base.notifications.config import NotificationConfig
            if not NotificationConfig.is_enabled():
                return
            recipients = cls._sync_recipients()
            if not recipients:
                return
            from base.notifications.telegram import TelegramAPI
            from base.notifications.helpers import format_datetime
            _, time_str = format_datetime()
            text = (
                f'<b>SYNC QABUL QILINDI</b>\n\n'
                f'Yangi: <b>{created}</b> ta\n'
                f'Yangilangan: <b>{updated}</b> ta\n'
                f'Branch: {get_branch_id()}\n'
                f'Vaqt: {time_str}'
            )
            TelegramAPI.send_message(text, chat_ids=recipients)
        except Exception as e:
            logger.debug(f'Pull notification skipped: {e}')

    @classmethod
    def _notify_error(cls, error):
        try:
            from base.notifications.config import NotificationConfig
            if not NotificationConfig.is_enabled():
                return
            recipients = cls._sync_recipients()
            if not recipients:
                return
            from base.notifications.telegram import TelegramAPI
            from base.notifications.helpers import format_datetime
            _, time_str = format_datetime()
            text = (
                f'<b>SYNC XATOLIK</b>\n\n'
                f'Xato: {error}\n'
                f'Branch: {get_branch_id()}\n'
                f'Vaqt: {time_str}'
            )
            TelegramAPI.send_message(text, chat_ids=recipients)
        except Exception as e:
            logger.debug(f'Sync error notification skipped: {e}')
