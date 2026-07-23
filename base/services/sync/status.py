from django.utils import timezone
from base.services.sync.cache import safe_get, safe_set, safe_delete


STATUS_KEY = 'sync:status'
STATUS_TTL = 86400


class SyncStatus:

    # The pull cursor key in the durable SyncState table.
    CURSOR_KEY = 'last_pull'
    SCOPE_EPOCH_KEY = 'sync_scope_epoch'
    # v2 adds OneToOne ownership repair, deterministic adoption of legacy
    # blank-branch roots, and durable recovery markers for quarantined rows.
    SCOPE_EPOCH = 'branch-target-v3'
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

                SyncState.objects.update_or_create(
                    key=cls.cursor_key(own_branch), defaults={'value': ''},
                )
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
        return safe_get(STATUS_KEY) or {}

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
