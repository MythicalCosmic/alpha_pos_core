from django.utils import timezone
from base.services.sync.cache import safe_get, safe_set, safe_delete


STATUS_KEY = 'sync:status'
STATUS_TTL = 86400


class SyncStatus:

    # The pull cursor key in the durable SyncState table.
    CURSOR_KEY = 'last_pull'
    SCOPE_EPOCH_KEY = 'sync_scope_epoch'
    PULL_CONTRACT_EPOCH_KEY = 'sync_pull_contract_epoch'
    PULL_REQUEST_GENERATION_KEY = 'sync_pull_request_generation'
    FULL_PULL_REQUESTED_AT_KEY = 'sync_full_pull_requested_at'
    FULL_PULL_COMPLETED_AT_KEY = 'sync_full_pull_completed_at'
    FULL_PULL_COMPLETED_GENERATION_KEY = (
        'sync_full_pull_completed_generation'
    )
    # v2 adds OneToOne ownership repair, deterministic adoption of legacy
    # blank-branch roots, and durable recovery markers for quarantined rows.
    SCOPE_EPOCH = 'branch-target-v3'
    # v3 adds the durable logical cloud-feed clock and fail-closed schema
    # handling on top of cloud-authoritative global records. Rewind each branch
    # once so rows skipped by an old wall-clock cursor or older schema contract
    # are delivered again.
    PULL_CONTRACT_EPOCH = 'cloud-authority-feed-clock-v3'
    QUARANTINE_KEY_PREFIX = 'sync_scope_quarantine:'
    ACTIVE_SCOPE_BRANCH_KEY = 'sync_scope_active_branch'

    @classmethod
    def _branch_state_key(cls, prefix, branch_id=None):
        """Keep mutable sync state isolated when BRANCH_ID changes."""
        from hashlib import sha256
        from base.services.sync.config import get_branch_id

        branch = str(
            get_branch_id() if branch_id is None else branch_id
        ).strip()
        digest = sha256(branch.encode('utf-8')).hexdigest()[:20]
        return f'{prefix}:{digest}'

    @classmethod
    def cursor_key(cls, branch_id=None):
        return cls._branch_state_key(cls.CURSOR_KEY, branch_id)

    @classmethod
    def scope_epoch_key(cls, branch_id=None):
        return cls._branch_state_key(cls.SCOPE_EPOCH_KEY, branch_id)

    @classmethod
    def pull_contract_epoch_key(cls, branch_id=None):
        return cls._branch_state_key(cls.PULL_CONTRACT_EPOCH_KEY, branch_id)

    @classmethod
    def pull_request_generation_key(cls, branch_id=None):
        return cls._branch_state_key(
            cls.PULL_REQUEST_GENERATION_KEY, branch_id,
        )

    @classmethod
    def full_pull_requested_at_key(cls, branch_id=None):
        return cls._branch_state_key(
            cls.FULL_PULL_REQUESTED_AT_KEY, branch_id,
        )

    @classmethod
    def full_pull_completed_at_key(cls, branch_id=None):
        return cls._branch_state_key(
            cls.FULL_PULL_COMPLETED_AT_KEY, branch_id,
        )

    @classmethod
    def full_pull_completed_generation_key(cls, branch_id=None):
        return cls._branch_state_key(
            cls.FULL_PULL_COMPLETED_GENERATION_KEY, branch_id,
        )

    @classmethod
    def dead_letter_revival_key(cls, branch_id=None):
        return cls._branch_state_key('sync_dl_revival_v2', branch_id)

    @classmethod
    def scope_quarantine_key(cls, model_class, record_uuid):
        """Stable SyncState key for one recoverable quarantined row."""
        from hashlib import sha256

        identity = f'{model_class._meta.label_lower}:{record_uuid}'.encode()
        digest = sha256(identity).hexdigest()[:40]
        return f'{cls.QUARANTINE_KEY_PREFIX}{digest}'

    @classmethod
    def _rewind_pull_locked(cls, SyncState, branch_id):
        """Rotate the replay generation and clear its cursor under one lock.

        Every cursor publisher locks this generation first too. That ordering
        makes a replay request win regardless of whether it races just before
        or just after an older pull attempts to publish its terminal frontier.
        """
        from uuid import uuid4

        generation, _ = (
            SyncState.objects.select_for_update().get_or_create(
                key=cls.pull_request_generation_key(branch_id),
                defaults={'value': ''},
            )
        )
        generation.value = uuid4().hex
        generation.save(update_fields=['value', 'updated_at'])
        cursor, _ = SyncState.objects.select_for_update().get_or_create(
            key=cls.cursor_key(branch_id),
            defaults={'value': ''},
        )
        if cursor.value:
            cursor.value = ''
            cursor.save(update_fields=['value', 'updated_at'])
        requested_at = timezone.now().isoformat()
        requested, _ = SyncState.objects.select_for_update().get_or_create(
            key=cls.full_pull_requested_at_key(branch_id),
            defaults={'value': ''},
        )
        requested.value = requested_at
        requested.save(update_fields=['value', 'updated_at'])
        completed, _ = SyncState.objects.select_for_update().get_or_create(
            key=cls.full_pull_completed_at_key(branch_id),
            defaults={'value': ''},
        )
        if completed.value:
            completed.value = ''
            completed.save(update_fields=['value', 'updated_at'])
        completed_generation, _ = (
            SyncState.objects.select_for_update().get_or_create(
                key=cls.full_pull_completed_generation_key(branch_id),
                defaults={'value': ''},
            )
        )
        if completed_generation.value:
            completed_generation.value = ''
            completed_generation.save(update_fields=['value', 'updated_at'])
        return generation.value

    @classmethod
    def restore_quarantined_target(cls, model_class, record):
        """Temporarily restore a quarantined row for its authoritative replay.

        Called inside the pull record transaction. The marker is deliberately
        retained until ``finish_quarantine_restore``: if FK resolution or a
        cash command defers, the caller rolls the transaction back and the row
        remains safely quarantined for the next replay.
        """
        from django.conf import settings

        if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'local':
            return None
        own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        incoming_branch = str(record.get('branch_id') or '').strip()
        record_uuid = record.get('uuid')
        if (
            not own_branch
            or incoming_branch != own_branch
            or not record_uuid
            or record.get('is_deleted')
        ):
            return None

        from base.models import SyncState

        key = cls.scope_quarantine_key(model_class, record_uuid)
        marker = SyncState.objects.select_for_update().filter(key=key).first()
        if marker is None:
            return None
        restored = model_class._base_manager.filter(
            uuid=record_uuid,
            is_deleted=True,
        ).update(is_deleted=False, branch_id=own_branch)
        if not restored:
            # The row may already have been restored manually. A successful
            # target replay can still retire the stale recovery marker.
            if not model_class._base_manager.filter(uuid=record_uuid).exists():
                return None
        return key

    @staticmethod
    def finish_quarantine_restore(marker_key):
        if not marker_key:
            return
        from base.models import SyncState
        SyncState.objects.filter(key=marker_key).delete()

    @classmethod
    def ensure_scope_epoch(cls):
        """One-time local cleanup/reset after the pull-routing policy change.

        The former feed delivered every *other* branch's transactions and
        excluded the terminal's own target rows. Merely changing the query
        leaves polluted rows in local analytics and an advanced cursor that
        skips old target commands. This atomic epoch transition:

        1. repairs child branch ids when all branch-owned FK/O2O parents agree;
        2. adopts unambiguous legacy blank-branch rows as local;
        3. quarantines remaining foreign rows with durable recovery markers;
        4. removes their outbound queue slots; and
        5. clears the cursor so the correctly scoped feed is replayed.

        Cloud is the aggregate source and must never run this local cleanup.
        """
        from django.conf import settings

        if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'local':
            return False
        own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if not own_branch:
            return False

        from django.db import transaction
        from django.db.utils import OperationalError, ProgrammingError

        try:
            with transaction.atomic():
                from base.models import SyncQueueRecord, SyncState

                active_branch, _ = (
                    SyncState.objects.select_for_update().get_or_create(
                        key=cls.ACTIVE_SCOPE_BRANCH_KEY,
                        defaults={'value': ''},
                    )
                )
                epoch, _ = SyncState.objects.select_for_update().get_or_create(
                    key=cls.scope_epoch_key(own_branch),
                    defaults={'value': ''},
                )
                branch_transition = active_branch.value != own_branch
                if (
                    epoch.value == cls.SCOPE_EPOCH
                    and not branch_transition
                ):
                    return False

                from base.services.sync.config import SYNC_ORDER, get_all_models
                models = get_all_models()
                branch_models = {
                    model for model in models.values()
                    if getattr(model, 'SYNC_PULL_SCOPE', 'branch') == 'branch'
                }

                # Parents are earlier in SYNC_ORDER. Two passes also resolve a
                # grandchild whose parent itself needed deterministic repair.
                for _pass in range(2):
                    changed = 0
                    for name in SYNC_ORDER:
                        model = models.get(name)
                        if model not in branch_models:
                            continue
                        parent_fields = [
                            field for field in model._meta.fields
                            if (
                                getattr(field, 'many_to_one', False)
                                or getattr(field, 'one_to_one', False)
                            )
                            and field.related_model in branch_models
                        ]
                        if not parent_fields:
                            continue
                        qs = model._base_manager.filter(is_deleted=False)
                        qs = qs.select_related(*[f.name for f in parent_fields])
                        for row in qs.iterator(chunk_size=500):
                            parent_branches = {
                                str(getattr(parent, 'branch_id', '') or '').strip()
                                for field in parent_fields
                                for parent in [getattr(row, field.name, None)]
                                if parent is not None
                                and str(
                                    getattr(parent, 'branch_id', '') or ''
                                ).strip()
                            }
                            if len(parent_branches) != 1:
                                continue
                            resolved = next(iter(parent_branches))
                            if str(row.branch_id or '').strip() != resolved:
                                model._base_manager.filter(pk=row.pk).update(
                                    branch_id=resolved,
                                )
                                changed += 1
                    if not changed:
                        break

                # Old pre-scope rows often have branch_id=''. On a local DB a
                # root with no branch-owned parent is deterministically node-
                # owned, so backfill it instead of terminally tombstoning it.
                # For children, adopt only when no nonblank parent contradicts
                # that conclusion; one agreeing parent was repaired above and
                # conflicting parents remain for recoverable quarantine.
                for name in SYNC_ORDER:
                    model = models.get(name)
                    if model not in branch_models:
                        continue
                    parent_fields = [
                        field for field in model._meta.fields
                        if (
                            getattr(field, 'many_to_one', False)
                            or getattr(field, 'one_to_one', False)
                        )
                        and field.related_model in branch_models
                    ]
                    blank = model._base_manager.filter(
                        is_deleted=False, branch_id='',
                    )
                    if not parent_fields:
                        blank.update(branch_id=own_branch)
                        continue
                    blank = blank.select_related(*[f.name for f in parent_fields])
                    adopt_ids = []
                    for row in blank.iterator(chunk_size=500):
                        parent_branches = {
                            str(getattr(parent, 'branch_id', '') or '').strip()
                            for field in parent_fields
                            for parent in [getattr(row, field.name, None)]
                            if parent is not None
                            and str(getattr(parent, 'branch_id', '') or '').strip()
                        }
                        if (
                            not parent_branches
                            or parent_branches == {own_branch}
                        ):
                            adopt_ids.append(row.pk)
                            if len(adopt_ids) == 500:
                                model._base_manager.filter(pk__in=adopt_ids).update(
                                    branch_id=own_branch,
                                )
                                adopt_ids = []
                    if adopt_ids:
                        model._base_manager.filter(pk__in=adopt_ids).update(
                            branch_id=own_branch,
                        )

                # Reverse dependency order is conservative for future models
                # with validation around parent state. QuerySet.update invokes
                # no delete/save hooks and therefore publishes no tombstones.
                # Every quarantined row gets a durable local marker. Clearing
                # the pull cursor then lets an authoritative target replay
                # restore a mistakenly tagged own row atomically.
                import json
                for name in reversed(SYNC_ORDER):
                    model = models.get(name)
                    if model not in branch_models:
                        continue
                    while True:
                        rows = list(
                            model._base_manager.filter(is_deleted=False)
                            .exclude(branch_id=own_branch)
                            .values_list('uuid', 'branch_id')[:500]
                        )
                        if not rows:
                            break
                        uuids = [row[0] for row in rows]
                        now = timezone.now().isoformat()
                        markers = [
                            SyncState(
                                key=cls.scope_quarantine_key(model, row_uuid),
                                value=json.dumps({
                                    'model': name,
                                    'model_label': model._meta.label_lower,
                                    'uuid': str(row_uuid),
                                    'original_branch_id': row_branch or '',
                                    'local_branch_id': own_branch,
                                    'reason': (
                                        'blank_or_conflicting_parent_scope'
                                        if not row_branch else 'foreign_branch_scope'
                                    ),
                                    'quarantined_at': now,
                                }),
                            )
                            for row_uuid, row_branch in rows
                        ]
                        SyncState.objects.bulk_create(markers, ignore_conflicts=True)
                        model._base_manager.filter(uuid__in=uuids).update(
                            is_deleted=True,
                        )
                        SyncQueueRecord.objects.filter(
                            model_name=name,
                            record_uuid__in=uuids,
                        ).delete()

                cls._rewind_pull_locked(SyncState, own_branch)
                epoch.value = cls.SCOPE_EPOCH
                epoch.save(update_fields=['value', 'updated_at'])
                active_branch.value = own_branch
                active_branch.save(update_fields=['value', 'updated_at'])
                return True
        except (OperationalError, ProgrammingError):
            # Called from post_migrate for each app; an early callback can run
            # before a later app's tables exist. A later callback/runtime pull
            # retries once the schema is complete.
            return False

    @classmethod
    def ensure_pull_contract_epoch(cls):
        """Durably request one complete cloud replay after a pull-policy change.

        The epoch is written in the same transaction as clearing the cursor. If
        the app or network fails immediately afterward, the blank cursor
        survives and every later background attempt starts from the beginning
        until a fully drained pull publishes a new cloud frontier.
        """
        from django.conf import settings

        if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'local':
            return False
        own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if not own_branch:
            return False

        from django.db import transaction
        from django.db.utils import OperationalError, ProgrammingError

        try:
            with transaction.atomic():
                from base.models import SyncState

                epoch, _ = SyncState.objects.select_for_update().get_or_create(
                    key=cls.pull_contract_epoch_key(own_branch),
                    defaults={'value': ''},
                )
                if epoch.value == cls.PULL_CONTRACT_EPOCH:
                    return False
                cls._rewind_pull_locked(SyncState, own_branch)
                epoch.value = cls.PULL_CONTRACT_EPOCH
                epoch.save(update_fields=['value', 'updated_at'])
                return True
        except (OperationalError, ProgrammingError):
            # Startup/post-migrate callers may run before SyncState exists.
            # The next ordinary pull retries this transition.
            return False

    @classmethod
    def request_full_pull(cls):
        """Atomically require a complete replay from a new generation."""
        from django.conf import settings

        if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'local':
            return False
        own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if not own_branch:
            return False
        from django.db import transaction
        from base.models import SyncState

        with transaction.atomic():
            cls._rewind_pull_locked(SyncState, own_branch)
        # This cache write is only a fast-path. Readers overlay the durable
        # SyncState markers, so a cache flush/restart or a late older completion
        # can never make a pending replay appear completed.
        cls.update(**cls.get_full_pull_status())
        return True

    @classmethod
    def get_pull_checkpoint(cls):
        """Return a cursor and the generation allowed to publish its result."""
        from uuid import uuid4
        from django.db import transaction
        from base.models import SyncState

        with transaction.atomic():
            generation, _ = (
                SyncState.objects.select_for_update().get_or_create(
                    key=cls.pull_request_generation_key(),
                    defaults={'value': uuid4().hex},
                )
            )
            if not generation.value:
                generation.value = uuid4().hex
                generation.save(update_fields=['value', 'updated_at'])
            cursor = SyncState.objects.filter(key=cls.cursor_key()).first()
            return (
                cursor.value if (cursor and cursor.value) else None,
                generation.value,
            )

    @classmethod
    def publish_pull_cursor(
        cls, value, generation, *, completes_full_pull=False,
    ):
        """Publish only if no newer replay request superseded this pull.

        The generation row is the serialization point shared with
        ``request_full_pull``. A compare followed by ``set_cursor`` in separate
        transactions would still have a check/write race and could erase the
        newly blank cursor.
        """
        from django.db import transaction
        from base.models import SyncState

        completed_at = None
        with transaction.atomic():
            current = (
                SyncState.objects.select_for_update()
                .filter(key=cls.pull_request_generation_key())
                .first()
            )
            if (
                current is None
                or not generation
                or current.value != generation
            ):
                return False
            cursor, _ = SyncState.objects.select_for_update().get_or_create(
                key=cls.cursor_key(),
                defaults={'value': ''},
            )
            cursor.value = value or ''
            cursor.save(update_fields=['value', 'updated_at'])
            if completes_full_pull:
                completed_at = timezone.now().isoformat()
                completed, _ = (
                    SyncState.objects.select_for_update().get_or_create(
                        key=cls.full_pull_completed_at_key(),
                        defaults={'value': ''},
                    )
                )
                completed.value = completed_at
                completed.save(update_fields=['value', 'updated_at'])
                completed_generation, _ = (
                    SyncState.objects.select_for_update().get_or_create(
                        key=cls.full_pull_completed_generation_key(),
                        defaults={'value': ''},
                    )
                )
                completed_generation.value = generation
                completed_generation.save(
                    update_fields=['value', 'updated_at'],
                )
        if completed_at:
            # Publish the cache copy only after the cursor and completion
            # markers committed together.
            cls.update(**cls.get_full_pull_status())
        return True

    @classmethod
    def get_full_pull_status(cls):
        """Return durable, branch-scoped full-replay progress.

        Cache state is deliberately not consulted. A replay request, its cursor
        rewind, and its completion marker share the same SyncState transaction,
        so this remains truthful across process restarts and cache loss.
        """
        from django.db.utils import OperationalError, ProgrammingError
        from base.models import SyncState

        keys = {
            'generation': cls.pull_request_generation_key(),
            'cursor': cls.cursor_key(),
            'requested_at': cls.full_pull_requested_at_key(),
            'completed_at': cls.full_pull_completed_at_key(),
            'completed_generation': (
                cls.full_pull_completed_generation_key()
            ),
        }
        try:
            rows = {
                row.key: row
                for row in SyncState.objects.filter(key__in=keys.values())
            }
        except (OperationalError, ProgrammingError):
            # Status is also read during early startup, before migrations may
            # have created SyncState. Preserve the cache-only fallback there.
            return {}

        generation_row = rows.get(keys['generation'])
        cursor_row = rows.get(keys['cursor'])
        requested_row = rows.get(keys['requested_at'])
        completed_row = rows.get(keys['completed_at'])
        completed_generation_row = rows.get(keys['completed_generation'])

        generation = generation_row.value if generation_row else None
        requested_at = requested_row.value if requested_row else None
        cursor = cursor_row.value if cursor_row else None
        if not requested_at and generation_row is not None and not cursor:
            # Upgrade recovery: earlier builds persisted the generation and
            # cursor rewind but kept the display timestamp only in cache.
            requested_at = generation_row.updated_at.isoformat()
        completed_at = completed_row.value if completed_row else None
        completed_generation = (
            completed_generation_row.value
            if completed_generation_row else None
        )
        completed = bool(
            requested_at
            and generation
            and completed_at
            and completed_generation == generation
        )
        pending = bool(requested_at and generation and not completed)
        state = (
            'pending' if pending
            else 'completed' if completed
            else 'not_requested'
        )
        return {
            'full_pull_requested_at': requested_at or None,
            'full_pull_completed_at': completed_at if completed else None,
            'full_pull_request_generation': generation or None,
            'full_pull_completed_generation': (
                completed_generation if completed else None
            ),
            'full_pull_pending': pending,
            'full_pull_state': state,
        }

    @classmethod
    def get_cursor(cls):
        """Durable pull cursor (cloud-clock `synced_at` frontier), or None.

        Stored in the DB (SyncState) rather than the cache so a restart or a
        cache flush can't silently reset it and trigger a full re-pull.
        """
        from base.models import SyncState
        row = SyncState.objects.filter(key=cls.cursor_key()).first()
        return row.value if (row and row.value) else None

    @classmethod
    def set_cursor(cls, value):
        from base.models import SyncState
        SyncState.objects.update_or_create(
            key=cls.cursor_key(), defaults={'value': value or ''},
        )

    @classmethod
    def update(cls, **kwargs):
        data = cls.get()
        data.update(kwargs)
        data['updated_at'] = timezone.now().isoformat()
        safe_set(STATUS_KEY, data, STATUS_TTL)

    @classmethod
    def get(cls):
        data = dict(safe_get(STATUS_KEY) or {})
        durable_full_pull = cls.get_full_pull_status()
        if durable_full_pull:
            data.update(durable_full_pull)
        requested = data.get('full_pull_request_generation')
        completed = data.get('full_pull_completed_generation')
        if (
            requested
            and completed != requested
            and data.get('full_pull_completed_at')
        ):
            # A stale pull can finish its cache update just after a newer
            # request commits. Generation tags keep it visibly pending.
            data = dict(data)
            data['full_pull_completed_at'] = None
        return data

    @classmethod
    def set_online(cls, online=True):
        cls.update(is_online=online)

    @classmethod
    def set_last_sync(cls, synced=0, failed=0, errors=None):
        cls.update(
            last_sync=timezone.now().isoformat(),
            last_synced_count=synced,
            last_failed_count=failed,
            last_error=errors[0] if errors else None,
        )

    @classmethod
    def set_last_pull(cls, created=0, updated=0, errors=None):
        # IMPORTANT: do NOT write `last_pull` here. `last_pull` is the durable
        # pull CURSOR — a cloud-clock `synced_at` frontier that pull_from_cloud
        # advances page by page and sends back as the `since` filter. Stamping
        # it with the terminal's local `now()` (this used to) clobbers that
        # cursor: with any clock skew between terminal and cloud, records the
        # cloud created between the true frontier and the terminal's clock are
        # silently skipped forever (a server-created user never arrives). This
        # field is a separate "when did we last finish a pull" status value.
        cls.update(
            last_pull_at=timezone.now().isoformat(),
            last_pull_created=created,
            last_pull_updated=updated,
            last_pull_error=errors[0] if errors else None,
        )

    @classmethod
    def set_error(cls, error):
        cls.update(last_error=str(error)[:500])

    @classmethod
    def clear(cls):
        safe_delete(STATUS_KEY)
