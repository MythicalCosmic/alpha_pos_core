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

                    result = send_batch(model_name, batch_data)

                    if result['success']:
                        # A 200 can still carry per-record failures (partial
                        # batch). Remove ONLY the records the receiver confirmed
                        # and keep the rest queued — purging the whole batch on
                        # the HTTP-200 silently lost the failed rows.
                        failed = set(result.get('failed_uuids') or [])
                        confirmed = [u for u in batch_uuids if u not in failed]
                        synced_uuids.extend(confirmed)
                        confirmed_by_model.setdefault(model_name, []).extend(confirmed)
                        total_synced += len(confirmed)
                        if failed:
                            failed_in_batch = [u for u in batch_uuids if u in failed]
                            total_failed += len(failed_in_batch)
                            SyncQueue.mark_batch_failed(
                                failed_in_batch,
                                'receiver rejected record(s) in a partial batch',
                                model_name=model_name,
                            )
                            msg = (f'{model_name}: {len(failed_in_batch)} of '
                                   f'{len(batch_uuids)} record(s) rejected by receiver')
                            errors.append(msg)
                            logger.warning(msg)
                        logger.info(f'Synced {len(confirmed)} {model_name} records')
                    else:
                        total_failed += len(batch)
                        error_msg = f'{model_name}: {result.get("error", "Unknown")}'
                        errors.append(error_msg)
                        SyncQueue.mark_batch_failed(
                            batch_uuids, result.get('error', 'Unknown'),
                            model_name=model_name,
                        )
                        logger.warning(f'Sync failed for {model_name}: {result.get("error")}')
                        stop_push = True
                        break

            if synced_uuids:
                # Remove per-model so a confirmed uuid only deletes that model's
                # queue row (the unique key is (model_name, record_uuid); two
                # models can share a record_uuid). confirmed_by_model already
                # carries the model→uuids breakdown the receiver confirmed.
                for mname, uuids in confirmed_by_model.items():
                    if uuids:
                        SyncQueue.remove(uuids, model_name=mname)

            # Stamp synced_at on the local rows the cloud confirmed so
            # objects.unsynced() is a meaningful "not yet pushed" signal — both
            # for status_report and the orphan-reconcile sweep. .update() bypasses
            # save(), so it won't bump sync_version or re-null synced_at; a later
            # edit to the row sets synced_at=None again and re-queues it normally.
            if confirmed_by_model:
                models_map = get_all_models()
                now = timezone.now()
                for mname, uuids in confirmed_by_model.items():
                    mclass = models_map.get(mname)
                    if mclass and uuids:
                        mclass.objects.filter(uuid__in=uuids).update(synced_at=now)

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

        pull_token = cls._acquire_lock('pull')
        if not pull_token:
            return {'success': False, 'message': 'Pull already in progress'}

        try:
            if not check_health():
                SyncStatus.set_online(False)
                return {'success': False, 'message': 'Cannot reach cloud server', 'offline': True}

            SyncStatus.set_online(True)

            cursor = SyncStatus.get_cursor()

            models = get_all_models()
            total_created = 0
            total_updated = 0
            errors = []
            # Records deferred because a required FK parent hadn't been applied
            # yet, keyed by model class. Retried after paging completes so a
            # child fetched on an earlier page than its parent isn't lost.
            deferred_by_model = {}
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
                        total_created, total_updated, [str(e) for e in errors[:1]]
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
                    # Fully drained — advance the persisted cursor to the
                    # server's snapshot time.
                    if server_ts:
                        SyncStatus.set_cursor(server_ts)
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
                    break

                # The frontier is "complete up to next_since"; persisting it now
                # makes the paging crash-safe (a mid-loop failure resumes here
                # instead of re-pulling from the start). Re-applies are
                # idempotent upserts.
                SyncStatus.set_cursor(next_since)
                cursor = next_since
            else:
                logger.warning('Pull: hit MAX_PAGES (%s); will resume next cycle', MAX_PAGES)

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
            if deferred_by_model:
                stuck = sum(len(v) for v in deferred_by_model.values())
                logger.warning(
                    'Pull: %d record(s) unresolved after retries (missing parent?)',
                    stuck,
                )
                errors.append(f'{stuck} record(s) unresolved (missing parent)')

            SyncStatus.set_last_pull(total_created, total_updated, [str(e) for e in errors[:1]])

            total = total_created + total_updated
            if total > 0 and not errors:
                cls._notify_pull_success(total_created, total_updated)
            elif errors:
                cls._notify_error(f'Pull errors: {errors[0]}')

            return {
                'success': True,
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
        """Re-queue any unsynced row that isn't already in the sync queue.

        Backstop for the on_commit enqueue path: if _queue_for_sync ever fails
        (and it swallows the exception), the row is saved with synced_at=NULL
        but never enqueued. This sweep, run at the top of push(), makes the
        queue self-healing without a separate cron. Bounded work after the first
        run because push() now stamps synced_at on confirmed records.
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
                queued = SyncQueue.queued_uuids_for_model(name)
                for obj in qs.iterator():
                    if str(obj.uuid) in queued:
                        continue
                    SyncQueue.add(name, str(obj.uuid), obj.to_sync_dict())
                    requeued += 1
            except Exception as e:
                logger.warning('reconcile unsynced failed for %s: %s', name, e)
        if requeued:
            logger.info('Sync reconcile: re-queued %d orphaned record(s)', requeued)
        return requeued

    @classmethod
    def _apply_records(cls, model_class, records, source_branch=None):
        # `deferred` holds records that couldn't be applied yet because a
        # required FK parent isn't present (or a transient error hit). The pull
        # loop retries them once the rest of the change set has landed, so a
        # child pulled before its parent isn't lost when the cursor advances.
        from django.db import transaction
        results = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': [], 'deferred': []}
        for record in records:
            try:
                # Each record applies in its own transaction so the get()->
                # compare->save inside from_sync_dict (which now takes a
                # select_for_update row lock) is atomic against a concurrent
                # push receiver applying the same uuid. Without this the pull
                # path could read a row, then a concurrent writer commits, and
                # the pull's stale save() clobbers the newer version.
                with transaction.atomic():
                    _, action = model_class.from_sync_dict(record, branch_id=source_branch)
                if action == 'deferred':
                    results['deferred'].append(record)
                elif action in ('created', 'updated', 'skipped'):
                    results[action] += 1
            except Exception as e:
                results['errors'].append({
                    'uuid': record.get('uuid'),
                    'error': str(e),
                })
                results['deferred'].append(record)
                results['skipped'] += 1
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
