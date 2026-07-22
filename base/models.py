import uuid
from decimal import Decimal
from django.db import models, transaction
from django.db.models.functions import Now
from django.db.models.fields.files import FieldFile
from django.conf import settings


class SyncQuerySet(models.QuerySet):
    def unsynced(self):
        return self.filter(synced_at__isnull=True)

    def from_branch(self, branch_id):
        return self.filter(branch_id=branch_id)

    def active(self):
        return self.filter(is_deleted=False)


class SyncManager(models.Manager):
    def get_queryset(self):
        # Keep Django's ordinary identity/query semantics. Pull-feed scoping
        # and the one-time scope-epoch quarantine prevent peer data from
        # entering a terminal; silently filtering the default manager instead
        # breaks explicit cross-branch/admin queries and makes sync upserts miss
        # an existing UUID then attempt a duplicate INSERT.
        return SyncQuerySet(self.model, using=self._db)

    def unsynced(self):
        return self.get_queryset().unsynced()

    def active(self):
        return self.get_queryset().active()


class SyncMixin(models.Model):
    uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )
    synced_at = models.DateTimeField(null=True, blank=True, db_index=True)
    sync_version = models.PositiveIntegerField(default=1)
    is_deleted = models.BooleanField(default=False, db_index=True)
    branch_id = models.CharField(max_length=50, blank=True, default='', db_index=True)

    class Meta:
        abstract = True

    # Outbound opt-out: a SyncMixin subclass whose rows are per-branch state
    # (e.g. treasury balances kept by get_or_create-per-kind) sets this True so
    # save()/hard_delete() never enqueue them for the cloud. Distinct from
    # `_sync_ingest_disabled`, which blocks the inbound (receive) direction.
    _sync_local_only = False
    # Cloud change-feed opt-out for one-way branch -> cloud records such as
    # AuditLog. This is separate from receive: the cloud still collects them,
    # but never sends them back to terminals.
    _sync_pull_disabled = False
    # Write-once records may be inserted by sync but never updated/resurrected.
    _sync_append_only = False
    # Branch-owned data is delivered only to its owning branch. This safely
    # echoes a branch's own push (idempotent), delivers later cloud target
    # edits/commands, and—critically—never fans transactional rows into peers.
    # Shared catalog/config models opt into ``global`` below.
    SYNC_PULL_SCOPE = 'branch'

    def save(self, *args, **kwargs):
        syncing = kwargs.pop('_syncing', False)
        mode = None
        publish_cloud_change = False
        queue_local_change = False
        if not syncing:
            mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
            node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
            current_branch = str(self.branch_id or '').strip()
            if (
                self.pk is None
                and mode == 'cloud'
                and getattr(type(self), 'SYNC_PULL_SCOPE', 'branch') == 'branch'
                and not current_branch
            ):
                # A cloud node is an aggregator, not a transactional target.
                # In the current single-branch deployment an explicit setting
                # gives old/admin CRUD a safe target. Multi-branch deployments
                # leave it empty and must pass branch_id at the call site.
                target = str(getattr(
                    settings, 'CLOUD_DEFAULT_TARGET_BRANCH_ID', '',
                ) or '').strip()
                if not target or target.lower() == 'cloud':
                    raise ValueError(
                        f'{type(self).__name__} is branch-scoped; cloud creates '
                        'must pass branch_id or configure '
                        'CLOUD_DEFAULT_TARGET_BRANCH_ID for a single branch',
                    )
                self.branch_id = target
            elif not current_branch and node_branch:
                self.branch_id = node_branch
            if self.pk:
                self.sync_version += 1
            update_fields = kwargs.get('update_fields')
            content_changed = update_fields is None or any(
                f not in ['synced_at', 'sync_version'] for f in update_fields
            )
            if content_changed:
                if mode == 'local':
                    # Branch: mark pending so the push worker sends it to the hub.
                    self.synced_at = None
                elif mode == 'cloud':
                    # Commit-boundary publication: the content write itself is
                    # deliberately invisible to the timestamp cursor.  If we
                    # stamped ``synced_at`` before the surrounding transaction
                    # committed, /changes could take a later high-water mark
                    # while still seeing the old row, then skip this change
                    # forever after commit.  NULL is also crash-safe: if the
                    # process dies before the on_commit callback, /changes keeps
                    # serving NULL rows outside timestamp pagination (duplicate
                    # delivery is harmless; permanent loss is not).
                    self.synced_at = None
                    publish_cloud_change = True
            # When a caller restricts the write with update_fields, the in-memory
            # sync_version bump and synced_at change above must be added to that
            # list or Django silently drops them: the persisted version would
            # stall (so peers reject the next update via _should_replace) and
            # synced_at would stay non-NULL (so the reconcile sweep never resends
            # it). Force the sync bookkeeping columns into the written set.
            if update_fields is not None and self.pk:
                forced = {'sync_version'}
                if content_changed:
                    forced.add('synced_at')
                kwargs['update_fields'] = list(set(update_fields) | forced)
            if (content_changed and mode == 'local'
                    and not self._sync_local_only):
                from base.services.sync.config import SyncConfig
                queue_local_change = SyncConfig.is_enabled()

        if queue_local_change:
            # The content row and its DB-backed outbound slot are one durability
            # unit.  SYNC_ON_SAVE used to suppress this enqueue (default=False),
            # leaving a locally-edited cloud row discoverable only by a later
            # best-effort sweep.  A crash before that sweep lost the edit.  Queue
            # every local content mutation while sync is enabled and let the
            # worker schedule control *when* it is sent.
            using = kwargs.get('using') or self._state.db or 'default'
            with transaction.atomic(using=using):
                super().save(*args, **kwargs)
                from base.services.sync.service import SyncService
                SyncService.queue_record(self)
        else:
            super().save(*args, **kwargs)
        if publish_cloud_change:
            self._publish_synced_at_after_commit(using=self._state.db)

    def _publish_synced_at_after_commit(self, *, using=None):
        """Publish this exact committed version to the cloud change feed.

        The caller must already have persisted the content with ``synced_at``
        set to NULL.  The conditional update prevents an older transaction's
        late callback from stamping (and thereby acknowledging) a newer save of
        the same row.  ``synced_at__isnull=True`` also prevents an equal-version
        callback from overwriting a publication that already won the race.
        """
        from django.utils import timezone

        model = type(self)
        pk = self.pk
        sync_version = self.sync_version

        def publish():
            published_at = timezone.now()
            manager = model._base_manager
            if using:
                manager = manager.using(using)
            updated = manager.filter(
                pk=pk,
                sync_version=sync_version,
                synced_at__isnull=True,
            ).update(synced_at=published_at)
            if (updated and self.pk == pk
                    and self.sync_version == sync_version
                    and self.synced_at is None):
                self.synced_at = published_at

        # Content is already durable when this runs. A transient database error
        # in the publisher must not turn a successful API/save/receive operation
        # into a reported failure that callers may retry as if content rolled
        # back. Robust callbacks are logged by Django and leave synced_at=NULL;
        # the change feed's NULL safety lane will keep serving that version.
        transaction.on_commit(publish, using=using, robust=True)

    @staticmethod
    def _is_sync_on_save():
        from base.services.sync.cache import safe_get
        override = safe_get('sync:config:on_save')
        if override is not None:
            return override
        return getattr(settings, 'SYNC_ON_SAVE', False)

    def delete(self, *args, **kwargs):
        hard_delete = kwargs.pop('hard_delete', False)
        if hard_delete:
            # Route the legacy ``delete(hard_delete=True)`` API through the
            # tombstone-aware implementation. Several order services use this
            # spelling; calling Model.delete() directly made removed line items
            # survive on the cloud and keep inflating order totals there.
            return self.hard_delete(*args, **kwargs)
        else:
            self.is_deleted = True
            self.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])

    def hard_delete(self, *args, **kwargs):
        # Capture identity for a tombstone before we delete the row, then
        # enqueue a soft-delete sync record in the same transaction so peers
        # also remove the record. Without this, hard deletes on one branch
        # never propagate and leave dangling FK references on others.
        from base.services.sync.config import SyncConfig
        mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
        using = kwargs.get('using') or self._state.db or 'default'
        pending_tombstone = None
        # Unlike an ordinary save, a physical deletion cannot be discovered by
        # the later unsynced-row reconcile sweep. Therefore its tombstone must
        # be queued whenever local sync is enabled, even when SYNC_ON_SAVE is
        # deliberately false (the default).
        if (mode == 'local' and SyncConfig.is_enabled()
                and not self._sync_local_only and self.pk):
            try:
                from base.services.sync.service import SyncService
                model_name = self.__class__.__name__.lower()
                tombstone = self.to_sync_dict()
                tombstone['is_deleted'] = True
                tombstone['sync_version'] = (self.sync_version or 0) + 1
                uuid_val = str(self.uuid)
                pending_tombstone = (SyncService, model_name, uuid_val, tombstone)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to prepare tombstone for {self.__class__.__name__} pk={self.pk}",
                    exc_info=True,
                )
                # A physical delete without its only durable sync marker would
                # permanently diverge the peer. Fail closed: leave the source
                # row intact so the operation can be retried safely.
                raise

        # The source row and its durable queue record live in the same database,
        # so they must commit as one unit.  Deferring the enqueue to on_commit
        # allowed a queue write failure to leave a permanently unannounced
        # physical deletion.  SyncQueue.add() uses a nested atomic block, which
        # safely composes with this transaction: either both writes commit or
        # the deletion rolls back and can be retried.
        with transaction.atomic(using=using):
            result = super().delete(*args, **kwargs)
            if pending_tombstone is not None:
                service, model_name, uuid_val, tombstone = pending_tombstone
                service.queue_tombstone(model_name, uuid_val, tombstone)
            return result

    def _queue_for_sync(self):
        try:
            from base.services.sync.service import SyncService
            SyncService.queue_record(self)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to queue {self.__class__.__name__} pk={self.pk} for sync",
                exc_info=True,
            )

    def to_sync_dict(self):
        data = {
            'uuid': str(self.uuid),
            'sync_version': self.sync_version,
            'is_deleted': self.is_deleted,
            'branch_id': self.branch_id,
        }
        for field in self._meta.get_fields():
            if field.concrete and not field.is_relation:
                if field.name not in [
                    'id', 'uuid', 'synced_at', 'sync_version', 'is_deleted', 'branch_id'
                ]:
                    value = getattr(self, field.name, None)
                    if hasattr(value, 'isoformat'):
                        value = value.isoformat()
                    elif isinstance(value, Decimal):
                        value = str(value)
                    elif isinstance(value, uuid.UUID):
                        value = str(value)
                    elif isinstance(value, FieldFile):
                        # FileField values are FieldFile objects, which the JSON
                        # sync queue cannot serialize. Replicate only the private
                        # storage-relative name; file bytes remain node-local and
                        # are served exclusively by the authenticated endpoint.
                        value = value.name or ''
                    data[field.name] = value
        return data

    # Direction-aware deny-lists for fields the sync ingestion must not
    # *overwrite*. Which list applies depends on the receiver's data-flow
    # direction (DEPLOYMENT_MODE):
    #   * SYNC_WRITE_DENYLIST — money/balance fields a branch owns and the cloud
    #     collects. Refused when a *branch* ingests (a pulled peer record must
    #     not rewrite local financials); accepted by the *cloud* (the trusted
    #     single-operator collector — otherwise revenue never aggregates).
    #   * SYNC_DENY_FROM_BRANCH — catalog/admin fields the cloud owns (e.g.
    #     Product.price). Refused when the *cloud* ingests a branch push;
    #     accepted by a *branch* pulling from the cloud.
    # On CREATE, direction matters. A branch pulling a trusted brand-new row must
    # keep its financial fields (there is no local value to protect). The cloud
    # receiving an untrusted branch create still strips protected fields that
    # have defaults; only required/no-default fields pass so the row can exist.
    SYNC_WRITE_DENYLIST = frozenset()
    SYNC_DENY_FROM_BRANCH = frozenset()
    # Fields supplied by a branch only when the row is first created on the
    # cloud.  They describe immutable producer evidence (for example an
    # Order's POS/QR origin), so a rolling older terminal may omit or replay a
    # default value but can never rewrite the value already accepted by the
    # hub.  Cloud-authored updates still flow down to branches normally.
    SYNC_CREATE_ONLY_FROM_BRANCH = frozenset()
    # Fields whose branch -> cloud ownership ends at settlement.  Models use
    # this for values which the originating till may legitimately establish
    # while an event is open, but must never time-travel after the cloud has
    # accepted the financial event (for example an Order's cashier and paid
    # totals).  Field names are expanded to their FK attnames by
    # ``_sync_frozen_from_branch_fields`` so scalar and relationship writes are
    # governed by the same rule.
    SYNC_IMMUTABLE_FROM_BRANCH_WHEN_PAID = frozenset()

    # Natural keys that uniquely identify a record independent of its uuid.
    # When an incoming sync record's uuid isn't found locally but another row
    # already owns the same natural-key value (e.g. User.email is unique), we
    # reconcile onto that row — adopting the incoming uuid — instead of blindly
    # INSERTing a duplicate that trips the DB unique constraint and gets
    # silently dropped by _apply_records (permanent loss of a server-created
    # user). Empty by default.
    SYNC_NATURAL_KEYS = ()

    @classmethod
    def _find_by_natural_key(cls, data, resolved_fks=None, incoming_branch=None):
        """Find an existing row by SYNC_NATURAL_KEYS so an incoming record with a
        new uuid reconciles onto it instead of INSERTing a duplicate that trips a
        unique constraint. A key can be a plain field (read from `data`) or an FK
        field (read from `resolved_fks`, the already-resolved related instances) —
        e.g. ShiftPaymentTotal's ('shift', 'method')."""
        keys = getattr(cls, 'SYNC_NATURAL_KEYS', ())
        if not keys:
            return None
        resolved_fks = resolved_fks or {}
        lookup = {}
        for k in keys:
            if k == 'branch_id':
                value = incoming_branch or data.get(k)
                if not value:
                    return None
                lookup[k] = value
                continue
            if k in resolved_fks:
                lookup[k] = resolved_fks[k]
                continue
            value = data.get(k)
            if value in (None, ''):
                return None
            lookup[k] = value
        return cls.objects.filter(**lookup).first()

    @classmethod
    def _effective_denylist(cls, mode=None):
        """Fields refused on this ingest, chosen by data-flow direction.

        Cloud receivers (mode='cloud') protect catalog/admin fields a branch
        push must not rewrite; branch receivers (mode='local') protect the
        money/balance fields a peer record must not rewrite.
        """
        if mode is None:
            mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
        if mode == 'cloud':
            denied = set(getattr(cls, 'SYNC_DENY_FROM_BRANCH', frozenset()))
            if getattr(cls, 'SYNC_PULL_SCOPE', 'branch') == 'global':
                # A globally pulled model is centrally owned configuration, not
                # branch-owned transaction data. Keep the per-model denylist for
                # documentation, but make the security property complete by
                # automatically covering every concrete catalog field (including
                # FK names/columns) and the soft-delete bit. Otherwise adding a
                # future mutable field silently creates a branch-token privilege
                # escalation until somebody remembers to update a hand-written
                # list.
                denied.add('is_deleted')
                for field in cls._meta.concrete_fields:
                    denied.add(field.name)
                    denied.add(field.attname)
            return frozenset(denied)
        return frozenset(getattr(cls, 'SYNC_WRITE_DENYLIST', frozenset()))

    @classmethod
    def _sync_required_no_default(cls, field_name):
        # True when the column is NOT NULL with no usable default — stripping it
        # on CREATE would raise IntegrityError, so create-time ingest keeps it.
        from django.db.models.fields import NOT_PROVIDED
        try:
            f = cls._meta.get_field(field_name)
        except Exception:
            return False
        if not hasattr(f, 'null') or f.null:
            return False
        if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
            return False
        return f.default is NOT_PROVIDED

    @classmethod
    def _strip_sync_denied(cls, data, *, creating=False, mode=None):
        denied = cls._effective_denylist(mode)
        if not denied:
            return data
        import logging
        logger = logging.getLogger(__name__)
        effective_mode = mode or getattr(settings, 'DEPLOYMENT_MODE', 'local')
        cleaned = {}
        for key, value in data.items():
            if key in denied:
                if creating and effective_mode == 'local':
                    # Trusted cloud -> branch create. There is no existing local
                    # money value to protect; dropping fields with model defaults
                    # turns real orders into zero-value placeholders.
                    cleaned[key] = value
                    continue
                if creating and cls._sync_required_no_default(key):
                    # Required column on a brand-new row — keep the origin value
                    # so the record can be inserted; nothing local to protect.
                    cleaned[key] = value
                    continue
                logger.warning(
                    'sync ingest: dropping denylisted field %s on %s (mode=%s)',
                    key, cls.__name__, effective_mode,
                )
                continue
            cleaned[key] = value
        return cleaned

    @classmethod
    def _strip_sync_branch_rewrites(cls, instance, data, *, mode=None):
        """Protect producer-owned fields on branch -> cloud updates.

        ``CloudReceiver`` is the normal push path, while a few maintenance and
        test paths call ``from_sync_dict`` directly.  Keeping the declaration
        here makes the ownership rule identical in both paths instead of
        relying on one transport-specific guard.
        """
        effective_mode = mode or getattr(settings, 'DEPLOYMENT_MODE', 'local')
        if effective_mode != 'cloud':
            return data

        import logging
        values = dict(data)
        for field_name in getattr(
            cls, 'SYNC_CREATE_ONLY_FROM_BRANCH', frozenset(),
        ):
            if field_name in values:
                logging.getLogger(__name__).warning(
                    'sync ingest: refused create-only rewrite of %s.%s uuid=%s',
                    cls.__name__, field_name, getattr(instance, 'uuid', None),
                )
                values.pop(field_name, None)

        for field_name in getattr(
            cls, 'SYNC_IMMUTABLE_FROM_BRANCH_AFTER_SET', frozenset(),
        ):
            if field_name not in values:
                continue
            current = getattr(instance, field_name, None)
            incoming = values[field_name]
            if current not in (None, '', {}, []) and incoming != current:
                logging.getLogger(__name__).warning(
                    'sync ingest: refused rewrite of immutable %s.%s uuid=%s',
                    cls.__name__, field_name, getattr(instance, 'uuid', None),
                )
                values.pop(field_name, None)

        for field_name in cls._sync_frozen_from_branch_fields(
            instance, mode=effective_mode,
        ):
            if field_name in values:
                logging.getLogger(__name__).warning(
                    'sync ingest: refused settled rewrite of %s.%s uuid=%s',
                    cls.__name__, field_name, getattr(instance, 'uuid', None),
                )
                values.pop(field_name, None)
        return values

    @classmethod
    def _sync_frozen_from_branch_fields(cls, instance, *, mode=None):
        """Concrete field names/attnames frozen after financial settlement."""
        effective_mode = mode or getattr(settings, 'DEPLOYMENT_MODE', 'local')
        if effective_mode != 'cloud' or not getattr(instance, 'is_paid', False):
            return frozenset()
        frozen = set(getattr(
            cls, 'SYNC_IMMUTABLE_FROM_BRANCH_WHEN_PAID', frozenset(),
        ))
        expanded = set(frozen)
        for name in frozen:
            try:
                field = cls._meta.get_field(name)
            except Exception:  # noqa: BLE001 - stale declarations stay safe
                continue
            expanded.add(field.name)
            expanded.add(field.attname)
        return frozenset(expanded)

    @classmethod
    def _repair_equal_version_sync(
        cls, instance, incoming_version, incoming_data, incoming_branch,
    ):
        """Apply a narrowly-scoped equal-version convergence repair.

        Normal conflict resolution never lets an equal version overwrite a
        row. Models may override this hook for one immutable rollout field;
        it runs only after UUID ownership/branch validation and under the same
        row lock as ordinary sync ingestion.
        """
        return False

    @classmethod
    def _pop_sync_automatic_values(cls, data):
        """Remove and parse source values Django would otherwise overwrite.

        ``auto_now``/``auto_now_add`` are not limited to the conventional
        created_at/updated_at names. Financial event models also use fields
        such as Inkassa.period_end and DiscountUsage.used_at. Preserving only
        two hard-coded names moves offline events into the receive window and
        corrupts time-series analytics.
        """
        import logging

        values = {}
        for field in cls._meta.concrete_fields:
            if not (getattr(field, 'auto_now', False)
                    or getattr(field, 'auto_now_add', False)):
                continue
            if field.name not in data:
                continue
            raw_value = data.pop(field.name)
            if raw_value in (None, ''):
                continue
            try:
                value = field.to_python(raw_value)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    'sync ingest: invalid automatic timestamp %s=%r on %s: %s',
                    field.name, raw_value, cls.__name__, exc,
                )
                continue
            if value is not None:
                values[field.name] = value
        return values

    @classmethod
    def _restore_sync_automatic_values(cls, instance, values, *, creating):
        """Restore source event times after save() has applied local clocks."""
        allowed = cls._strip_sync_denied(values, creating=creating)
        if not allowed:
            return
        cls.objects.filter(pk=instance.pk).update(**allowed)
        for field_name, value in allowed.items():
            setattr(instance, field_name, value)

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone
        from django.apps import apps
        from base.services.sync.config import FK_UUID_MAPPINGS

        data = data.copy()
        uuid_val = data.pop('uuid')
        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        # The kwarg `branch_id` comes from the verified bearer-token mapping
        # (push) or from the trusted cloud connection (pull). If the payload
        # tries to override it with a different value, that's either misrouting
        # or a forgery attempt — drop the record rather than silently pinning
        # it to the wrong branch.
        payload_branch = data.pop('branch_id', None)
        if payload_branch and branch_id and payload_branch != branch_id:
            import logging
            logging.getLogger(__name__).warning(
                'sync ingest: payload branch_id=%s mismatched auth branch_id=%s; '
                'dropping record %s on %s',
                payload_branch, branch_id, uuid_val, cls.__name__,
            )
            return None, 'skipped'
        incoming_branch = payload_branch or branch_id

        # Resolve UUID-keyed FK references to local instances. The push/receive
        # path does this in CloudReceiver._resolve_foreign_keys, but the
        # pull-from-cloud path lands straight here — so a model without an
        # explicit from_sync_dict override (Table, Shift, CashReconciliation,
        # and most stock/HR models) would silently drop every FK to NULL,
        # Integrity-erroring on non-nullable FKs (place, user, shift) or losing
        # the association. Resolving in the default fixes it for every model.
        # Each model's to_sync_dict only emits the *_uuid keys for its own FKs,
        # so iterating the shared mapping over `data` can't cross-wire fields.
        resolved_fks = {}
        for uuid_field, (app_label, model_name, fk_field) in FK_UUID_MAPPINGS.items():
            if uuid_field not in data:
                continue
            try:
                fk_model_field = cls._meta.get_field(fk_field)
            except Exception:
                # FK_UUID_MAPPINGS is shared by every synced model; ignore
                # synthetic keys whose target field is not on this class.
                continue
            uuid_value = data.pop(uuid_field)
            if uuid_value in (None, ''):
                if fk_model_field.null:
                    # Explicit null means clear the relationship.  Merely
                    # omitting the *_uuid key still leaves it unchanged.
                    resolved_fks[fk_field] = None
                    continue
                if is_deleted:
                    # An existing tombstone does not need its old parent to be
                    # present; a never-seen tombstone has nothing to delete.
                    if cls.objects.filter(uuid=uuid_val).exists():
                        continue
                    return None, 'skipped'
                import logging
                logging.getLogger(__name__).warning(
                    'sync ingest: required FK %s explicitly cleared on %s; '
                    'deferring record %s',
                    fk_field, cls.__name__, uuid_val,
                )
                return None, 'deferred'
            if not uuid_value:
                continue
            try:
                related = apps.get_model(app_label, model_name).objects.filter(
                    uuid=uuid_value,
                ).first()
            except Exception:
                related = None
            if related is not None:
                parent_scope = getattr(
                    type(related), 'SYNC_PULL_SCOPE', 'branch',
                )
                parent_branch = str(getattr(related, 'branch_id', '') or '')
                if (
                    parent_scope == 'branch'
                    and parent_branch != str(incoming_branch or '')
                ):
                    # A known parent owned by another branch is a permanent
                    # feed-scope violation, not an ordering problem. ACK it as
                    # skipped so it cannot poison every later pull.
                    import logging
                    logging.getLogger(__name__).warning(
                        'sync ingest: refused cross-branch FK %s=%s owner=%s '
                        'incoming=%s on %s uuid=%s',
                        fk_field, uuid_value, parent_branch, incoming_branch,
                        cls.__name__, uuid_val,
                    )
                    return None, 'skipped'
                resolved_fks[fk_field] = related
            else:
                if is_deleted:
                    if cls.objects.filter(uuid=uuid_val).exists():
                        continue
                    return None, 'skipped'
                # Supplied parent UUID means the relationship is intentional,
                # even for a nullable column. Defer until it arrives rather than
                # silently materializing NULL and advancing the pull cursor.
                import logging
                logging.getLogger(__name__).warning(
                    'sync ingest: unresolved %s FK %s=%s on %s; deferring '
                    'record %s for retry',
                    'nullable' if fk_model_field.null else 'required',
                    fk_field, uuid_value, cls.__name__, uuid_val,
                )
                return None, 'deferred'

        # Capture every source automatic timestamp before save() replaces it
        # with the receiver's clock. updated_at remains available to conflict
        # resolution below; the full set is restored after the write.
        automatic_values = cls._pop_sync_automatic_values(data)
        incoming_updated = automatic_values.get('updated_at')

        try:
            # Row-lock the existing row for the get()->compare->save sequence so
            # a concurrent pull/receiver applying the same uuid can't interleave
            # and clobber a newer version (lost update). select_for_update is
            # only valid inside a transaction — the pull path wraps each record
            # in transaction.atomic(); when called outside one (e.g. unit tests)
            # fall back to a plain get() rather than raising.
            from django.db import transaction
            base_qs = cls.objects
            if transaction.get_connection().in_atomic_block:
                base_qs = cls.objects.select_for_update()
            instance = base_qs.get(uuid=uuid_val)
            if (
                getattr(cls, 'SYNC_PULL_SCOPE', 'branch') == 'branch'
                and incoming_branch
                and str(instance.branch_id or '') != str(incoming_branch)
            ):
                # A scoped feed must never revise a UUID already owned by a
                # different branch.  The service-level target guard protects
                # normal pulls; this model-level check also covers direct
                # callers and corrupted/legacy feeds without mutating evidence.
                import logging
                logging.getLogger(__name__).warning(
                    'sync ingest: refused %s uuid=%s owner=%s incoming=%s',
                    cls.__name__, uuid_val, instance.branch_id, incoming_branch,
                )
                return instance, 'skipped'
            if cls._repair_equal_version_sync(
                instance,
                sync_version,
                {**data, 'updated_at': incoming_updated},
                incoming_branch,
            ):
                return instance, 'updated'
            # Refuse to resurrect a hard-deleted row's slot via an older
            # incoming payload. (Soft-deletes are handled by is_deleted
            # propagation; this branch only fires for live rows.)
            should = cls._should_replace(
                instance, sync_version,
                {**data, 'updated_at': incoming_updated},
                incoming_branch,
            )
            if not should:
                return instance, 'skipped'
            # A locally-tombstoned row is terminal — never resurrect it.
            if instance.is_deleted and not is_deleted:
                return instance, 'skipped'
            update_values = cls._strip_sync_denied(data, creating=False)
            update_values = cls._strip_sync_branch_rewrites(
                instance, update_values,
            )
            for key, value in update_values.items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            settled_frozen = cls._sync_frozen_from_branch_fields(instance)
            for fk_field, related in resolved_fks.items():
                try:
                    model_field = cls._meta.get_field(fk_field)
                    fk_names = {model_field.name, model_field.attname}
                except Exception:  # noqa: BLE001
                    fk_names = {fk_field}
                if fk_names.isdisjoint(settled_frozen):
                    setattr(instance, fk_field, related)
            instance.sync_version = sync_version
            if 'is_deleted' not in settled_frozen:
                instance.is_deleted = is_deleted
            instance.synced_at = timezone.now()
            instance.save(_syncing=True)
            cls._restore_sync_automatic_values(
                instance, automatic_values, creating=False,
            )
            return instance, 'updated'
        except cls.DoesNotExist:
            # uuid not present locally. Before INSERTing, check whether a
            # different local row already owns one of this model's natural keys
            # (e.g. a server-created user whose email matches an existing local
            # user). If so, reconcile onto that row — converging on the incoming
            # uuid — rather than INSERTing a duplicate that would raise
            # IntegrityError and be silently dropped, never to retry.
            natural = cls._find_by_natural_key(
                data, resolved_fks, incoming_branch=incoming_branch,
            )
            if natural is not None:
                instance = natural
                instance.uuid = uuid_val
                # Reconcile onto an existing row → an UPDATE: protect denied
                # fields just like the version-matched update branch.
                update_values = cls._strip_sync_denied(data, creating=False)
                update_values = cls._strip_sync_branch_rewrites(
                    instance, update_values,
                )
                for key, value in update_values.items():
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                settled_frozen = cls._sync_frozen_from_branch_fields(instance)
                for fk_field, related in resolved_fks.items():
                    try:
                        model_field = cls._meta.get_field(fk_field)
                        fk_names = {model_field.name, model_field.attname}
                    except Exception:  # noqa: BLE001
                        fk_names = {fk_field}
                    if fk_names.isdisjoint(settled_frozen):
                        setattr(instance, fk_field, related)
                instance.sync_version = sync_version
                if 'is_deleted' not in settled_frozen:
                    instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                instance.branch_id = incoming_branch or instance.branch_id or ''
                instance.save(_syncing=True)
                cls._restore_sync_automatic_values(
                    instance, automatic_values, creating=False,
                )
                return instance, 'updated'

            instance = cls(
                uuid=uuid_val,
                sync_version=sync_version,
                is_deleted=is_deleted,
                branch_id=incoming_branch or '',
                synced_at=timezone.now(),
            )
            for key, value in cls._strip_sync_denied(data, creating=True).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            for fk_field, related in resolved_fks.items():
                setattr(instance, fk_field, related)
            instance.save(_syncing=True)
            cls._restore_sync_automatic_values(
                instance, automatic_values, creating=True,
            )
            return instance, 'created'

    @classmethod
    def _should_replace(cls, instance, incoming_version, incoming_data, incoming_branch):
        # Higher sync_version always wins — that's normal forward progress and
        # must apply regardless of where the record came from.
        if incoming_version > instance.sync_version:
            return True
        if incoming_version < instance.sync_version:
            return False

        # Equal version == a genuine conflict (both sides independently reached
        # the same version). Resolution is OWNERSHIP-aware, not blanket
        # "local always wins" — otherwise a till that bumped a record's version
        # locally (e.g. last_login_at on login) would REJECT an authoritative
        # cloud change to that same record (a password reset, a price edit) on a
        # version tie. Deterministic; no dependence on cross-machine clock skew.
        mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
        if mode == 'cloud':
            # The hub accepts the branch push (branches own their transactional
            # data). Cloud-owned fields are still protected by the direction-aware
            # SYNC_DENY_FROM_BRANCH denylist, so this can't rewrite credentials.
            return True
        if getattr(cls, 'SYNC_PULL_SCOPE', 'branch') == 'global':
            # Global catalog/configuration is cloud-owned. A local edit may
            # independently reach the same version, but it is never allowed to
            # win the tie and make an authoritative cloud menu/price/user update
            # disappear. Branch pushes for these models are refused by the hub.
            return True
        # On a branch: keep our row ONLY when it's THIS branch's OWN record
        # (orders, drawer, the till's transactions) — that's the "local
        # dominant" intent. For records owned elsewhere (cloud catalog/users,
        # branch_id != ours) the originating node is authoritative, so accept the
        # incoming change instead of silently dropping it.
        own_branch = getattr(settings, 'BRANCH_ID', '') or ''
        local_branch = instance.branch_id or ''
        return local_branch != own_branch

    @classmethod
    def _is_sync_denylisted(cls, field_name):
        denied = getattr(cls, 'SYNC_WRITE_DENYLIST', frozenset())
        return field_name in denied


class User(SyncMixin, models.Model):
    # Cloud-managed staff identities are shared configuration. In production
    # they intentionally use branch_id='cloud'; operational ownership comes
    # from the active Shift, not from rewriting the identity on each terminal.
    SYNC_PULL_SCOPE = 'global'
    class RoleChoices(models.TextChoices):
        USER = "USER", "User"
        ADMIN = "ADMIN", "Admin"
        CASHIER = "CASHIER", "Cashier"
        # Monoblock-level manager: logs in on the POS next to cashiers (NOT in
        # the admin dashboard like ADMIN), but with elevated in-app access
        # (settings, etc.). Gated server-side via role_required('MANAGER').
        MANAGER = "MANAGER", "Manager"
        WAITER = "WAITER", "Waiter"
        # Mobile courier identities authenticate through couriers.courier_required
        # only. They must never inherit CASHIER/POS permissions merely because
        # both products use the same Session table.
        COURIER = "COURIER", "Courier"
        # Kitchen staff. Created without a password (non-login label — used for
        # kitchen attribution / KDS), so it never appears in the cashier login
        # picker (get_pos_staff admits only CASHIER/MANAGER) and can't sign in.
        CHEF = "CHEF", "Chef"

    class UserStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    first_name = models.CharField(max_length=25)
    last_name = models.CharField(max_length=25)
    email = models.EmailField()
    password = models.CharField(max_length=128)

    role = models.CharField(
        max_length=10,
        choices=RoleChoices.choices,
        default=RoleChoices.USER,
    )

    status = models.CharField(
        max_length=10,
        choices=UserStatus.choices,
        default=UserStatus.ACTIVE,
    )

    permissions = models.JSONField(default=list, blank=True)

    last_login_at = models.DateTimeField(null=True, blank=True)
    last_login_api = models.CharField(max_length=20, null=True, blank=True)
    # Account creation time (auto-stamped on INSERT, like every other model).
    # Added late via migration 0035; the pre-existing rows were backfilled to the
    # migration run time (no historical creation timestamp was ever recorded).
    created_at = models.DateTimeField(auto_now_add=True)
    # Required by SyncMixin._should_replace tiebreaker: when two branches
    # land at the same sync_version, the row with the newer updated_at
    # wins. Without this field, equal-version syncs fell through to a
    # branch_id comparison that wasn't deterministic for User updates.
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    # Central user management: the owner creates/edits cashiers and admins on
    # the cloud hub, which is the trusted source of truth and DISTRIBUTES these
    # fields to every terminal. So a *branch* pulling from the cloud (mode=
    # 'local') must ACCEPT credentials — SYNC_WRITE_DENYLIST is empty.
    #
    # The forgery threat is the reverse direction: a holder of a *branch* token
    # pushing UP to the hub must not be able to flip a cashier to ADMIN, rewrite
    # a password hash, or suspend/delete a user. That push lands on the cloud
    # (mode='cloud'), so those fields go in SYNC_DENY_FROM_BRANCH — refused on
    # the cloud-receive direction, still distributed downward on pull. (On
    # CREATE a required NOT-NULL field with no default is still written — see
    # _strip_sync_denied — so a brand-new user row can still materialize.)
    SYNC_WRITE_DENYLIST = frozenset()
    SYNC_DENY_FROM_BRANCH = frozenset({'role', 'permissions', 'password', 'status', 'is_deleted'})

    # Email is unique, so it's the natural key used to reconcile a server-
    # created user against an existing local row with a different uuid (e.g. a
    # bootstrap admin) instead of dropping it on an IntegrityError.
    SYNC_NATURAL_KEYS = ('email',)

    class Meta:
        constraints = [
            # Soft-deleted users still occupy their row. A *global* unique index
            # on email would block reusing an address after a user is deleted and
            # raise IntegrityError exactly where the app-level check (which only
            # looks at live rows) reported the email as free. Scope uniqueness to
            # non-deleted rows.
            models.UniqueConstraint(
                fields=['email'],
                # Live rows with a non-empty email only (blank emails aren't unique).
                condition=models.Q(is_deleted=False) & ~models.Q(email=''),
                name='uniq_user_email_active',
            ),
        ]

    objects = SyncManager()

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Session(models.Model):
    user_id = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, db_index=True)
    ip_address = models.CharField(max_length=45)
    user_agent = models.CharField(max_length=256, null=True, blank=True, default='')
    payload = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    last_activity = models.DateTimeField(auto_now_add=True)
    # Absolute expiry. Set by the auth services on login. A NULL value
    # would indicate either a legacy pre-migration row or a buggy code path
    # that bypassed the auth service; in both cases the session must be
    # treated as expired so it forces re-authentication on the next request.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['payload']),
        ]

    def is_expired(self):
        if self.expires_at is None:
            return True
        from django.utils import timezone
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f"Session {self.pk} - user {self.user_id_id}"


class Category(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    name = models.CharField(max_length=50)
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='children',
    )
    sort_order = models.IntegerField(default=0)
    colors = models.JSONField(default=list, blank=True, help_text="Colors: ['#e74c3c', '#3498db']")
    status = models.CharField(
        max_length=10,
        choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')],
        default='ACTIVE',
    )
    slug = models.SlugField()
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Scope slug uniqueness to live rows so a slug freed by soft-deleting
            # a category can be reused (generate_unique_slug only checks live
            # rows; a global unique index would IntegrityError on the dead row).
            models.UniqueConstraint(
                fields=['slug'],
                # Only enforce uniqueness for live rows with a NON-empty slug —
                # many categories legitimately have no slug ('' is not unique).
                condition=models.Q(is_deleted=False) & ~models.Q(slug=''),
                name='uniq_category_slug_active',
            ),
        ]

    # Reconcile an incoming category onto an existing local row with the same
    # (non-empty) slug instead of INSERTing a duplicate that fails the unique
    # constraint ("UNIQUE constraint failed: base_category.slug"). Empty slugs
    # are skipped by _find_by_natural_key, so slug-less categories still insert.
    SYNC_NATURAL_KEYS = ('slug',)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['parent_category_uuid'] = str(self.parent.uuid) if self.parent else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        # A malformed self-reference cannot ever resolve into a valid tree.
        # All ordinary parent resolution (including defer-until-parent-arrives)
        # belongs to the generic sync contract.
        data = data.copy()
        if str(data.get('parent_category_uuid') or '') == str(data.get('uuid') or ''):
            data['parent_category_uuid'] = None
        return super().from_sync_dict(data, branch_id=branch_id)

    def __str__(self):
        return self.name


class Product(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products",
        db_index=True,
    )
    colors = models.JSONField(default=list, blank=True, help_text="Colors: ['#e74c3c', '#3498db']")
    name = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # Soliq IKPU / SPIC / MXIK classification code (from tasnif.soliq.uz).
    # Required by the OFD for live fiscalization; blank is tolerated in
    # mock/sandbox so the catalog can be coded gradually.
    ikpu_code = models.CharField(max_length=17, blank=True, default='')
    # Instant items (drinks, packaged goods) need no kitchen preparation:
    # their order items are auto-readied the moment the order is created and
    # they're excluded from the kitchen / chef display.
    is_instant = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    # Price is catalog data the cloud owns: a branch pulling from the cloud must
    # accept it, but a branch *push* must not rewrite it on the cloud (admin /
    # audited path only). So it's denied on the cloud-receive direction, not on
    # branch ingest — see SyncMixin._effective_denylist.
    SYNC_WRITE_DENYLIST = frozenset()
    SYNC_DENY_FROM_BRANCH = frozenset({'price'})

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['category_uuid'] = str(self.category.uuid) if self.category else None
        return data

    def __str__(self):
        return self.name


class DeliveryPerson(SyncMixin, models.Model):
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50, null=True, blank=True)
    phone_number = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Place(SyncMixin, models.Model):
    class PlaceType(models.TextChoices):
        HALL = "HALL", "Hall"
        BAR = "BAR", "Bar"
        TERRACE = "TERRACE", "Terrace"
        PRIVATE_ROOM = "PRIVATE_ROOM", "Private Room"
        OUTDOOR = "OUTDOOR", "Outdoor"

    name = models.CharField(max_length=100)
    place_type = models.CharField(
        max_length=15,
        choices=PlaceType.choices,
        default=PlaceType.HALL,
    )
    capacity = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_place_type_display()})"


class Table(SyncMixin, models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        OCCUPIED = "OCCUPIED", "Occupied"
        RESERVED = "RESERVED", "Reserved"
        OUT_OF_SERVICE = "OUT_OF_SERVICE", "Out of Service"

    place = models.ForeignKey(
        Place,
        on_delete=models.CASCADE,
        related_name="tables",
    )
    number = models.CharField(max_length=20)
    capacity = models.PositiveIntegerField(default=4)
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['place', 'sort_order', 'number']
        constraints = [
            # Soft-deleted tables keep their row; scope (place, number)
            # uniqueness to live rows so a number freed by deleting a table can
            # be reused (number_exists only checks live rows).
            models.UniqueConstraint(
                fields=['place', 'number'],
                # Live tables with a non-empty number only.
                condition=models.Q(is_deleted=False) & ~models.Q(number=''),
                name='uniq_table_place_number_active',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['place_uuid'] = str(self.place.uuid) if self.place else None
        return data

    def __str__(self):
        return f"Table {self.number} ({self.place.name})"


class Customer(SyncMixin, models.Model):
    """A client/customer an order can be attributed to — created from the
    desktop POS (by phone) AND reconciled from the Telegram mini-app
    (smartfood.Customer) at dispatch. Syncs branch<->cloud by uuid like
    DeliveryPerson, so the client id on an order is visible everywhere.

    `is_staff` marks a customer who is also a staff member (e.g. an employee
    placing a personal order) — used for staff-discount / reporting splits."""
    name = models.CharField(max_length=120, blank=True, default='')
    phone_number = models.CharField(max_length=20, blank=True, default='', db_index=True)
    email = models.EmailField(blank=True, default='')
    # Links a base.Customer back to the Telegram mini-app customer it came from,
    # so the server can reconcile smartfood orders onto the same client.
    telegram_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    # A customer who is also staff (employee personal orders, staff pricing).
    is_staff = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return self.name or self.phone_number or f'Customer #{self.pk}'

    @staticmethod
    def normalize_phone(phone):
        """Reduce a phone to a comparable key: digits only, with the UZ country
        code. '+998 90 123-45-67', '998901234567', and '901234567' all collapse
        to '998901234567', so the same human matches across desktop + Telegram."""
        from base.services.phone import normalize_uz_phone
        return normalize_uz_phone(phone)

    def save(self, *args, **kwargs):
        """Persist a stable digits-only identity regardless of write path."""
        normalized = self.normalize_phone(self.phone_number)
        changed = normalized != (self.phone_number or '')
        self.phone_number = normalized
        if changed and kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = list(
                set(kwargs['update_fields']) | {'phone_number'}
            )
        return super().save(*args, **kwargs)

    @classmethod
    def resolve(
        cls, phone=None, telegram_id=None, name=None, *, branch_id=None,
        create=True, adopt_node_owned=False,
    ):
        """Resolve one customer identity, optionally within a branch.

        Desktop callers can omit ``branch_id`` because their manager is already
        branch-scoped. Cloud order creators must pass the owning shift's branch:
        Customer is branch-scoped and an Order that points at a cloud-owned
        Customer cannot be applied on the terminal. ``create=False`` allows an
        authentication flow to link an existing customer without creating such
        a placeholder. During the single-branch transition,
        ``adopt_node_owned=True`` may move an old blank/cloud placeholder to the
        explicitly requested branch, but never steals another branch's row.

        Phone is the cross-channel key and is matched before Telegram ID.
        Existing identity fields are backfilled but never overwritten. Returns
        ``(customer, created)``; customer is ``None`` when no match exists and
        ``create`` is false.
        """
        name = (name or '').strip()[:120]
        # Use the canonical key for exact indexed lookup AND persistence. The
        # normalized scan remains for legacy rows written before this contract.
        phone = cls.normalize_phone(phone)
        norm = phone
        requested_branch = (
            None if branch_id is None else str(branch_id or '').strip()
        )
        all_qs = cls.objects.filter(is_deleted=False)
        if transaction.get_connection().in_atomic_block:
            all_qs = all_qs.select_for_update()

        def find_match(queryset):
            customer = None
            if phone:
                customer = queryset.filter(
                    phone_number=phone,
                ).order_by('id').first()
            if customer is None and norm:
                for cid, cphone in queryset.exclude(
                    phone_number='',
                ).values_list('id', 'phone_number'):
                    if cls.normalize_phone(cphone) == norm:
                        customer = queryset.get(id=cid)
                        break
            if customer is None and telegram_id:
                customer = queryset.filter(
                    telegram_id=telegram_id,
                ).order_by('id').first()
            return customer

        qs = all_qs
        if requested_branch is not None:
            qs = qs.filter(branch_id=requested_branch)
        customer = find_match(qs)

        adopted = False
        if (
            customer is None
            and requested_branch
            and adopt_node_owned
            and getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
        ):
            node_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
            placeholder_branches = ['']
            if node_branch and node_branch != requested_branch:
                placeholder_branches.append(node_branch)
            customer = find_match(
                all_qs.filter(branch_id__in=placeholder_branches)
            )
            if customer is not None:
                customer.branch_id = requested_branch
                adopted = True

        if customer is None:
            if not create:
                return None, False
            values = {
                'name': name,
                'phone_number': phone,
                'telegram_id': telegram_id or None,
            }
            if requested_branch is not None:
                values['branch_id'] = requested_branch
            return cls.objects.create(**values), True

        changed = adopted
        if telegram_id and not customer.telegram_id:
            customer.telegram_id = telegram_id
            changed = True
        if phone and (
            not customer.phone_number
            or cls.normalize_phone(customer.phone_number) == norm
        ) and customer.phone_number != phone:
            customer.phone_number = phone
            changed = True
        if name and not customer.name:
            customer.name = name
            changed = True
        if changed:
            customer.save()
        return customer, False


class Order(SyncMixin, models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        COMPLETED = "COMPLETED", "Completed"
        CANCELED = "CANCELED", "Canceled"

    class OrderType(models.TextChoices):
        HALL = "HALL", "Hall (Dine-in)"
        DELIVERY = "DELIVERY", "Delivery"
        PICKUP = "PICKUP", "Pickup"

    class Origin(models.TextChoices):
        """Durable producer of the order.

        This is intentionally independent of ``order_type``: a Telegram order
        can be DELIVERY or PICKUP, while a QR order is usually HALL.  Keeping a
        stable origin on the synced Order lets tills react to remote orders
        without guessing from placeholder users, notes, or delivery state.
        """

        POS = "POS", "POS"
        QR = "QR", "QR"
        TELEGRAM = "TELEGRAM", "Telegram"

    delivery_person = models.ForeignKey(
        DeliveryPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
    )

    place = models.ForeignKey(
        Place,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )
    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    cashier = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="handled_orders",
    )
    # The client this order is for (desktop: by phone; Telegram: reconciled from
    # smartfood.Customer at dispatch). Nullable — legacy/walk-in orders have none.
    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        db_index=True,
    )

    display_id = models.IntegerField(default=1)
    # The kitchen-line number shown on the CHEF display. Unlike display_id (which
    # wraps at 100 so the cashier/receipt number stays short), this counter only
    # ever increases, so the chef never sees the number "reset" mid-service.
    # Per-branch (allocated from ChefQueueCounter), nullable for legacy rows.
    chef_queue_number = models.IntegerField(null=True, blank=True)
    # Stable per-BUSINESS-DAY sequence (item 4): the human order reference on the
    # admin Orders page. Unlike display_id it never wraps (so two orders the same
    # day never share a number), and unlike chef_queue_number it resets to 1 each
    # business day. Per-branch, allocated at creation, synced as a value (the
    # counter itself never propagates). Nullable: legacy rows + tills not yet on a
    # build that assigns it — the FE falls back to display_id when null.
    order_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    order_type = models.CharField(
        max_length=10,
        choices=OrderType.choices,
        default=OrderType.HALL,
    )
    order_origin = models.CharField(
        max_length=16,
        choices=Origin.choices,
        default=Origin.POS,
        db_index=True,
    )

    phone_number = models.CharField(max_length=20, null=True, blank=True)
    # Dedicated destination shared by POS, customer history, courier, and sync.
    # The UI sends a clean human-readable address string; keep the empty wire
    # value stable as '' for hall/pickup orders and legacy rows.
    delivery_address = models.TextField(default='', blank=True)
    description = models.TextField(null=True, blank=True)

    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        UZCARD = "UZCARD", "Uzcard"
        HUMO = "HUMO", "Humo"
        # Acquirer-agnostic card. smartfood already writes this literal, and a till
        # may emit it instead of picking Uzcard/Humo. Reporting folds UZCARD/HUMO/CARD
        # into one `card` tender (see base.services.tender); the stored value keeps
        # whatever acquirer detail the operator captured.
        CARD = "CARD", "Card"
        PAYME = "PAYME", "Payme"
        # Set on the Order when a single sale is split across >1 distinct method.
        # The per-line breakdown lives in OrderPayment rows. NEVER a reporting bucket
        # and never a valid INPUT method — see base.services.tender.
        MIXED = "MIXED", "Mixed"

    is_paid = models.BooleanField(default=False, db_index=True)
    # Stable identity of the atomic checkout action which settled this order.
    # A UUID survives HTTP retries, queue retries and UUID changes on individual
    # OrderPayment rows.  Nullable preserves rolling compatibility with orders
    # produced by pre-action-id terminals; new payment writers populate it in
    # the same transaction as the header and tender lines.
    payment_action_id = models.UUIDField(
        null=True, blank=True, unique=True, editable=False,
    )
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        null=True, blank=True,
        db_index=True,
    )
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Pay-time percent discount (0..100) applied to total_amount at checkout.
    # The amount due the cashier collects is total_amount * (1 - pct/100).
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # Operational volume/preparation reports filter by creation time.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    # Revenue, tender, product-sales, shift, and anomaly reporting uses the
    # settlement event. This index keeps those range queries off a growing heap.
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Database-local commit cursor for Inkassa batches. ``paid_at`` is the
    # economic event time and may arrive from an offline branch long after that
    # business-time window was collected. This cursor is stamped under the
    # branch CashRegister lock and never synchronized.
    accounting_recorded_at = models.DateTimeField(
        null=True, blank=True, editable=False,
    )
    # Waiter "send to cashier" signal: stamped when a waiter asks the cashier to
    # collect payment for their (unpaid) order. Advisory only — the cashier
    # screen highlights orders with this set and is_paid=False; it is implicitly
    # superseded once the order is paid. Waiter and cashier share the same till,
    # so this is a local UI workflow signal and is NOT synced (popped below,
    # like display_id) — the cloud has no use for it.
    payment_requested_at = models.DateTimeField(null=True, blank=True)

    objects = SyncManager()

    # Refuse sync ingestion of payment / total fields. The cloud is the
    # collector of these; a peer cannot dictate "this order is paid for
    # 99999". Field-level guard, not row-level — the rest of the order
    # (status transitions, item changes) still syncs normally.
    SYNC_WRITE_DENYLIST = frozenset({
        'is_paid', 'payment_method', 'total_amount', 'subtotal',
        'discount_amount', 'discount_percent', 'paid_at',
        'accounting_recorded_at',
    })
    SYNC_DENY_FROM_BRANCH = frozenset({'accounting_recorded_at'})
    # Origin is producer evidence, accepted when a branch first creates the
    # order but never rewritten by later branch pushes.  In particular, an old
    # till that only knows the POS default cannot downgrade a cloud TELEGRAM or
    # QR origin during status/payment updates.
    SYNC_CREATE_ONLY_FROM_BRANCH = frozenset({'order_origin'})
    # Once the cloud has accepted a paid header, later/stale branch versions
    # may still advance operational state (READY/COMPLETED) but can no longer
    # rewrite the economic event or move it to another cashier/shift.
    SYNC_IMMUTABLE_FROM_BRANCH_WHEN_PAID = frozenset({
        'is_paid', 'payment_method', 'subtotal', 'discount_amount',
        'discount_percent', 'total_amount', 'paid_at', 'cashier', 'is_deleted',
    })
    # A rolling-upgrade retry may backfill a previously blank action identity,
    # but an established identity is immutable even before header repair marks
    # the order paid.
    SYNC_IMMUTABLE_FROM_BRANCH_AFTER_SET = frozenset({'payment_action_id'})

    @classmethod
    def branch_sync_create_allowed(cls, *, uuid_val, values, resolved_fks):
        """A till may originate POS/QR orders, never impersonate Telegram."""
        return values.get('order_origin', cls.Origin.POS) in {
            cls.Origin.POS,
            cls.Origin.QR,
        }

    def to_sync_dict(self):
        data = super().to_sync_dict()
        # display_id is a per-branch counter value (DisplayIdCounter) meaning
        # "the number shown on THIS branch's screen". It must never propagate or
        # two branches' orders would overwrite each other's numbers and the
        # local get_by_display_id lookup would see duplicates. Keep it local.
        data.pop('display_id', None)
        # chef_queue_number is a per-branch monotonic counter (ChefQueueCounter),
        # same as display_id it must stay local or two branches' kitchen numbers
        # would collide on pull.
        data.pop('chef_queue_number', None)
        # Local waiter→cashier UI signal (see field comment) — never synced.
        data.pop('payment_requested_at', None)
        data.pop('accounting_recorded_at', None)
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        data['cashier_uuid'] = str(self.cashier.uuid) if self.cashier else None
        data['delivery_person_uuid'] = str(self.delivery_person.uuid) if self.delivery_person else None
        data['place_uuid'] = str(self.place.uuid) if self.place else None
        data['table_uuid'] = str(self.table.uuid) if self.table else None
        data['customer_uuid'] = str(self.customer.uuid) if self.customer else None
        data['delivery_address'] = self.delivery_address or ''
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        data = data.copy()
        data.pop('accounting_recorded_at', None)
        return super().from_sync_dict(data, branch_id=branch_id)

    @classmethod
    def _repair_equal_version_sync(
        cls, instance, incoming_version, incoming_data, incoming_branch,
    ):
        # Rolling terminals may already hold a server order at the same
        # version with the pre-field POS default. Repair only that legacy
        # POS -> remote transition. Never rewrite QR <-> TELEGRAM.
        incoming_origin = incoming_data.get('order_origin')
        if not (
            getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud'
            and incoming_version == instance.sync_version
            and instance.order_origin == cls.Origin.POS
            and incoming_origin in {cls.Origin.QR, cls.Origin.TELEGRAM}
        ):
            return False
        cls.objects.filter(pk=instance.pk).update(order_origin=incoming_origin)
        instance.order_origin = incoming_origin
        return True

    class Meta:
        indexes = [
            models.Index(
                fields=['branch_id', 'accounting_recorded_at'],
                name='order_branch_acct_idx',
            ),
        ]

    def save(self, *args, **kwargs):
        """Stamp the first paid accounting cursor under the branch owner lock."""
        from base.services.phone import normalize_uz_phone

        normalized_phone = normalize_uz_phone(self.phone_number)
        canonical_phone = normalized_phone or None
        phone_changed = canonical_phone != self.phone_number
        self.phone_number = canonical_phone
        if phone_changed and kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = list(
                set(kwargs['update_fields']) | {'phone_number'}
            )
        if not (
            self.is_paid
            and self.paid_at is not None
            and self.accounting_recorded_at is None
        ):
            return super().save(*args, **kwargs)

        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            from django.utils import timezone
            from base.services.accounting_cursor import lock_branch_accounting

            register = lock_branch_accounting(self.branch_id or None)
            branch_added = False
            if not self.branch_id:
                self.branch_id = register.branch_id
                branch_added = True
            self.accounting_recorded_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                fields = set(update_fields)
                fields.add('accounting_recorded_at')
                if branch_added:
                    fields.add('branch_id')
                kwargs['update_fields'] = list(fields)
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.display_id} - {self.order_type} - {self.status}"


class OrderItem(SyncMixin, models.Model):
    order = models.ForeignKey(
        Order,
        related_name="items",
        on_delete=models.CASCADE,
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
    )
    quantity = models.PositiveIntegerField()
    detail = models.TextField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        data['product_uuid'] = str(self.product.uuid) if self.product else None
        return data

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class CashRegister(SyncMixin, models.Model):
    """The physical till balance for one branch.

    A cloud database holds a synchronized copy for every branch, so selecting a
    global ``.first()`` is never valid. The partial unique constraint makes the
    ownership invariant explicit: one live register per branch; soft-deleted
    history may remain for audit/recovery.
    """
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Cumulative value of cloud-issued cash-out commands (inkassa, cashbox
    # expenses, and refunds) this branch has applied to ``current_balance``.
    # The two values are saved/synced on the
    # same row, which lets the cloud derive an offline-safe available balance:
    #
    #   reported balance - (issued command total - applied command total)
    #
    # A cumulative amount (rather than a separate acknowledgement row) avoids
    # an ordering window where the acknowledgement arrives before the updated
    # balance and briefly makes the same cash collectable twice.
    remote_cash_out_applied_total = models.DecimalField(
        max_digits=18, decimal_places=2, default=0,
    )
    last_updated = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    # A branch owns its drawer. Cloud receives the amount, but a pull must not
    # overwrite the till's local balance. branch_id is also the sync identity so
    # independently-created rows converge rather than violating the constraint.
    SYNC_WRITE_DENYLIST = frozenset({
        'current_balance', 'remote_cash_out_applied_total',
    })
    SYNC_NATURAL_KEYS = ('branch_id',)

    class Meta:
        db_table = 'cash_register'
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id'],
                condition=models.Q(is_deleted=False),
                name='uniq_cash_register_active_branch',
            ),
            models.CheckConstraint(
                condition=models.Q(is_deleted=True) | ~models.Q(branch_id=''),
                name='cash_register_active_branch_required',
            ),
        ]

    def __str__(self):
        return f"Cash Register: {self.current_balance}"


class Inkassa(SyncMixin, models.Model):
    # Kept in notes as well as this field during the desktop transition.  An
    # older desktop safely stores the marker even though it does not know the
    # new column; its upgrade migration can then discover and apply the pending
    # command.  API serializers strip the marker from operator-visible notes.
    REGISTER_COMMAND_MARKER = '[ALPHAPOS_REGISTER_COMMAND_V1]'
    # Cloud refunds travel as ordinary OrderRefund accounting events plus one
    # companion Inkassa cash-out command. Inkassa existed on every supported
    # desktop, so its generic marker survives a pull performed before upgrade;
    # this second marker lets new code hide the transport row from collection
    # history and period boundaries.
    REFUND_COMMAND_MARKER = '[ALPHAPOS_REFUND_CASH_COMMAND_V1]'

    cashier = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inkassas",
    )

    class InkassType(models.TextChoices):
        CASH = "CASH", "Cash"
        UZCARD = "UZCARD", "Uzcard"
        HUMO = "HUMO", "Humo"
        CARD = "CARD", "Card"
        PAYME = "PAYME", "Payme"

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    inkass_type = models.CharField(
        max_length=10,
        choices=InkassType.choices,
    )

    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(auto_now_add=True)
    total_orders = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # True only for a new, cloud-issued instruction to remove physical CASH
    # from the owning branch register. Historical inkassa rows default False,
    # so deploying this protocol never re-applies old collections.
    register_command = models.BooleanField(default=False)
    # Treasury allocation under the reconciliation-first lifecycle:
    #   settlement_offset_amount = this collection already recognized in SAFE
    #     by per-shift manager reconciliation (audit/physical movement only)
    #   legacy_treasury_amount = provably unrecognized excess posted to SAFE now
    # Their sum equals amount for rows allocated by upgraded code. Historical
    # rows remain unstamped because they were processed by the legacy lifecycle.
    settlement_offset_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )
    legacy_treasury_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )
    treasury_allocated_at = models.DateTimeField(null=True, blank=True)
    # Shared by every tender row created by one manager collection request.
    # Non-cash collection requires a client-supplied key; the conditional
    # unique constraint below makes service-level retries idempotent even when
    # they bypass the HTTP response cache.
    collection_batch_key = models.CharField(max_length=128, blank=True, default='')
    collection_payload_hash = models.CharField(max_length=64, blank=True, default='')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()
    _sync_append_only = True

    # Inkassa is a cash-history record. The amounts, balances, and
    # collected-revenue numbers must never be set from a peer push — they
    # are only ever computed locally at performance time. Sync should
    # propagate the *existence* of an Inkassa event, not let a peer dictate
    # its financial figures.
    SYNC_WRITE_DENYLIST = frozenset({
        'amount', 'balance_before', 'balance_after', 'total_revenue',
        'settlement_offset_amount', 'legacy_treasury_amount',
        'treasury_allocated_at',
    })
    # A branch may create ordinary local history, but only the cloud admin path
    # may turn a row into an authoritative cash-removal command.
    SYNC_DENY_FROM_BRANCH = frozenset({
        'amount', 'inkass_type', 'register_command',
        'settlement_offset_amount',
        'legacy_treasury_amount', 'treasury_allocated_at',
        'collection_batch_key', 'collection_payload_hash',
    })

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id', 'collection_batch_key', 'inkass_type'],
                condition=~models.Q(collection_batch_key=''),
                name='uniq_inkassa_branch_batch_tender',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(treasury_allocated_at__isnull=True)
                    | models.Q(
                        amount=(
                            models.F('settlement_offset_amount')
                            + models.F('legacy_treasury_amount')
                        ),
                    )
                ),
                name='inkassa_allocated_amount_reconciles',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(settlement_offset_amount__gte=0)
                    & models.Q(legacy_treasury_amount__gte=0)
                ),
                name='inkassa_allocations_nonnegative',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['cashier_uuid'] = str(self.cashier.uuid) if self.cashier else None
        return data

    @classmethod
    def command_notes(cls, notes=''):
        """Encode a register command compatibly with pre-command desktops."""
        text = str(notes or '')
        return f'{cls.REGISTER_COMMAND_MARKER}\n{text}'

    @classmethod
    def visible_notes(cls, notes=''):
        """Return operator notes without the internal transition marker."""
        text = str(notes or '')
        prefix = f'{cls.REGISTER_COMMAND_MARKER}\n'
        return text[len(prefix):] if text.startswith(prefix) else text

    @classmethod
    def refund_command_notes(cls, refund, order, reason=''):
        """Encode a legacy-compatible cash-refund command with audit links."""
        detail = (
            f'{cls.REFUND_COMMAND_MARKER} '
            f'refund_uuid={refund.uuid} order_uuid={order.uuid}'
        )
        if reason:
            detail = f'{detail}\n{str(reason)}'
        return cls.command_notes(detail)

    @classmethod
    def refund_command_prefix(cls):
        return f'{cls.REGISTER_COMMAND_MARKER}\n{cls.REFUND_COMMAND_MARKER}'

    @classmethod
    def pending_register_amount(cls, register):
        """Cash commands issued by cloud but not in a branch balance report.

        Callers that make a financial decision hold a row lock on ``register``.
        Every command creator uses the same lock, so the issued/applied snapshot
        cannot race another collection decision.
        """
        from django.conf import settings
        from django.db.models import Q, Sum

        command_filter = Q(register_command=True)
        if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
            # Upgrade bridge: an old desktop retained the marker but did not
            # know the flag column yet. Cloud never trusts a branch-made marker.
            command_filter |= Q(notes__startswith=cls.REGISTER_COMMAND_MARKER)
        issued = (
            cls.objects.filter(
                branch_id=register.branch_id,
                is_deleted=False,
                inkass_type=cls.InkassType.CASH,
            )
            .filter(command_filter)
            .aggregate(total=Sum('amount'))['total']
            or Decimal('0')
        )

        # Cloud-created cashbox expenses use the same durable cash-out protocol
        # as inkassa. Import lazily to keep base/cashbox model loading acyclic.
        try:
            from django.apps import apps
            Expense = apps.get_model('cashbox', 'CashboxExpense')
            expense_filter = Q(register_command=True)
            if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
                expense_filter |= Q(
                    comment__startswith=Expense.REGISTER_COMMAND_MARKER,
                )
            issued += (
                Expense.objects.filter(
                    branch_id=register.branch_id,
                    is_deleted=False,
                )
                .filter(expense_filter)
                .aggregate(total=Sum('amount'))['total']
                or Decimal('0')
            )
        except (LookupError, AttributeError):
            # Supports the narrow migration window before cashbox's command
            # column exists; inkassa commands remain fully functional.
            pass

        # Refund cash is another physical cash-out. A locally performed refund
        # already changed the register and has register_command=False; a cloud
        # refund is an authoritative command applied by the owning branch.
        try:
            from django.apps import apps
            Refund = apps.get_model('base', 'OrderRefund')
            refund_filter = Q(register_command=True)
            if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
                refund_filter |= Q(
                    reason__startswith=Refund.REGISTER_COMMAND_MARKER,
                )
            issued += (
                Refund.objects.filter(
                    branch_id=register.branch_id,
                    is_deleted=False,
                )
                .filter(refund_filter)
                .aggregate(total=Sum('drawer_cash_amount'))['total']
                or Decimal('0')
            )
        except (LookupError, AttributeError):
            pass

        applied = register.remote_cash_out_applied_total or Decimal('0')
        return max(issued - applied, Decimal('0'))

    @classmethod
    def _apply_pending_register_commands(cls, branch_id):
        """Apply every known cloud CASH command to its owning local register.

        Pull applies one record inside a transaction. Summing all known command
        rows and comparing them with the cumulative amount on CashRegister makes
        the operation idempotent across duplicate pulls and process crashes.
        The register balance and acknowledgement total move in one row/save.
        """
        from decimal import Decimal
        import logging

        from django.conf import settings
        from django.db import transaction
        from django.utils import timezone

        if getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud':
            return True
        own_branch = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        branch = str(branch_id or '').strip()
        if not branch or branch != own_branch:
            return True

        with transaction.atomic():
            register = (
                CashRegister.objects.select_for_update()
                .filter(branch_id=branch, is_deleted=False)
                .first()
            )
            if register is None:
                register = CashRegister.objects.create(
                    branch_id=branch, current_balance=Decimal('0'),
                )
                register = CashRegister.objects.select_for_update().get(
                    pk=register.pk,
                )
            applied = register.remote_cash_out_applied_total or Decimal('0')
            command_total = applied + cls.pending_register_amount(register)
            delta = command_total - applied
            if delta <= 0:
                return True
            current_balance = register.current_balance or Decimal('0')
            if delta > current_balance:
                # A branch may spend cash after the cloud's last balance report
                # but before pulling this command. Never acknowledge money that
                # was not physically available: returning False makes the sync
                # pull retain its cursor and retry this durable command later.
                logging.getLogger(__name__).error(
                    'cash register %s cannot apply pending cash-out %s; '
                    'physical balance is %s. Leaving command pending.',
                    branch, delta, current_balance,
                )
                return False
            register.current_balance = current_balance - delta
            register.remote_cash_out_applied_total = command_total
            register.last_updated = timezone.now()
            register.save(update_fields=[
                'current_balance', 'remote_cash_out_applied_total', 'last_updated',
                'synced_at', 'sync_version',
            ])
            return True

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        instance, action = super().from_sync_dict(data, branch_id=branch_id)
        # A transition-era desktop may already hold this row with only the
        # marker in notes. Apply based on either representation even when
        # conflict resolution kept the existing row.
        if (
            instance is not None
            and not instance.is_deleted
            and instance.inkass_type == cls.InkassType.CASH
            and (
                instance.register_command
                or str(instance.notes or '').startswith(
                    cls.REGISTER_COMMAND_MARKER
                )
            )
        ):
            applied = cls._apply_pending_register_commands(instance.branch_id)
            if not applied:
                return instance, 'deferred'
        return instance, action

    def __str__(self):
        return f"Inkassa #{self.id} - {self.amount} on {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class TreasuryAccount(SyncMixin, models.Model):
    """A money pot the business holds outside the till drawer.

    SAFE = manager-confirmed shift proceeds. Every confirmed tender is posted
    here at reconciliation; a later inkassa is only the physical register
    movement/audit trail and must not recognize the proceeds a second time.
    BANK = explicit transfers and bank-funded expenses outside shift handover.
    One (soft-undeleted) row per kind; read/created via get_or_create.
    """
    class Kind(models.TextChoices):
        SAFE = 'SAFE', 'Safe (cash)'
        BANK = 'BANK', 'Bank (cards)'

    kind = models.CharField(max_length=10, choices=Kind.choices)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    last_updated = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    # Local-only: one get_or_create-per-kind row per branch, with a per-branch
    # balance mutated under a row lock in TreasuryService. There is no coherent
    # cross-branch identity for a "safe"/"bank" account, so syncing it would
    # create duplicate-kind rows on the cloud and clobber balances. Treasury is
    # per-branch state — never propagate it (matches Sequence/DisplayIdCounter).
    _sync_local_only = True
    _sync_ingest_disabled = True

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['kind'],
                condition=models.Q(is_deleted=False),
                name='uniq_active_treasury_account_kind',
            ),
        ]

    def __str__(self):
        return f"{self.kind}: {self.balance}"


class TreasuryTransaction(SyncMixin, models.Model):
    """Append-only ledger of every SAFE / BANK movement."""
    class Type(models.TextChoices):
        INKASSA = 'INKASSA', 'Inkassa deposit'
        TRANSFER_IN = 'TRANSFER_IN', 'Transfer in'
        TRANSFER_OUT = 'TRANSFER_OUT', 'Transfer out'
        FEE = 'FEE', 'Transfer fee'
        EXPENSE = 'EXPENSE', 'Expense'
        ADJUSTMENT = 'ADJUSTMENT', 'Adjustment'
        SUPPLIER_PAYMENT = 'SUPPLIER_PAYMENT', 'Supplier payment'
        SALARY_PAYMENT = 'SALARY_PAYMENT', 'Salary payment'
        SHIFT_DEPOSIT = 'SHIFT_DEPOSIT', 'Shift settlement deposit'

    account = models.ForeignKey(
        TreasuryAccount, on_delete=models.CASCADE, related_name='transactions',
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    # Signed change applied to the account balance (+ in / - out).
    delta = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    balance_before = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    # The other side of a transfer (null for inkassa / expense).
    counterparty = models.ForeignKey(
        TreasuryAccount, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='counterparty_transactions',
    )
    category = models.CharField(max_length=50, blank=True, default='')
    description = models.TextField(blank=True, default='')
    reference_type = models.CharField(max_length=50, blank=True, default='')
    reference_id = models.PositiveIntegerField(null=True, blank=True)
    performed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='treasury_transactions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    # Local-only: this ledger's `account` FK points at a per-branch treasury
    # account with no cross-branch identity, so the ledger can't propagate
    # coherently either. Per-branch state — never sync it. See TreasuryAccount.
    _sync_local_only = True
    _sync_ingest_disabled = True
    _sync_append_only = True

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One authoritative SAFE recognition per shift+tender.  The
            # reconciliation service also row-locks the Shift, but this keeps
            # the append-only ledger safe from any future writer that bypasses
            # that service.
            models.UniqueConstraint(
                fields=['reference_id', 'category'],
                condition=(
                    models.Q(type='SHIFT_DEPOSIT')
                    & models.Q(reference_type='ShiftSettlement')
                ),
                name='uniq_shift_tender_safe_post',
            ),
            models.UniqueConstraint(
                fields=['reference_id'],
                condition=(
                    models.Q(type='INKASSA')
                    & models.Q(reference_type='InkassaLegacy')
                ),
                name='uniq_legacy_inkassa_safe_post',
            ),
        ]

    def __str__(self):
        return f"{self.type} {self.delta} ({self.account_id})"

    def save(self, *args, **kwargs):
        if self.pk:
            raise TypeError('TreasuryTransaction is append-only and cannot be updated')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('TreasuryTransaction is append-only and cannot be deleted')

    def hard_delete(self, *args, **kwargs):
        raise TypeError('TreasuryTransaction is append-only and cannot be deleted')


def _default_business_day_start():
    """Operating-day opening default (07:00). A callable keeps the migration
    serialization stable and avoids a module-level datetime import."""
    from datetime import time
    return time(7, 0)


def _default_business_open():
    from datetime import time
    return time(7, 0)


def _default_business_close():
    from datetime import time
    return time(3, 0)


class AppSettings(models.Model):
    hr_enabled = models.BooleanField(default=False)
    waiter_enabled = models.BooleanField(default=False)
    # Operating-day cutover: stats/dashboards treat [business_day_start, next
    # business_day_start) as ONE business day, so a 01:00 sale counts toward the
    # night before. Per-restaurant; default 03:00. See base.services.business_day.
    business_day_start = models.TimeField(default=_default_business_day_start)
    # Working hours the venue actually trades — the FE's "Working hours" preset for
    # the time-of-day (tod_from/tod_to) dashboard filter. Reporting DEFAULT window,
    # not an enforcement gate. Per-restaurant; defaults 09:00 open / 23:00 close.
    business_open = models.TimeField(default=_default_business_open)
    business_close = models.TimeField(default=_default_business_close)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'app settings'
        verbose_name_plural = 'app settings'

    _CACHE_KEY = 'app_settings:v1'
    _CACHE_TTL = 60

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        # Bust the per-process cache so the next reader sees the new state.
        from django.core.cache import cache
        cache.delete(self._CACHE_KEY)

    @classmethod
    def load(cls):
        # Wrap the get_or_create with a short cache so every request that
        # reads the toggle (every HR/waiter view, etc.) doesn't pay a SELECT.
        from django.core.cache import cache
        cached = cache.get(cls._CACHE_KEY)
        if cached is not None:
            return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        cache.set(cls._CACHE_KEY, obj, cls._CACHE_TTL)
        return obj

    def __str__(self):
        return "App Settings"


class ShiftTemplate(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    name = models.CharField(max_length=100)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['start_time']

    def __str__(self):
        return f"{self.name} ({self.start_time} - {self.end_time})"


class Shift(SyncMixin, models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        # ENDED = cashier closed the shift; totals frozen, awaiting the
        # manager's cash reconciliation. COMPLETED = manager confirmed it.
        ENDED = 'ENDED', 'Ended'
        COMPLETED = 'COMPLETED', 'Completed'
        ABANDONED = 'ABANDONED', 'Abandoned'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shifts')
    shift_template = models.ForeignKey(
        ShiftTemplate, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='shifts',
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    total_orders = models.PositiveIntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_collected = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default='')
    # Stable install identity for CASHIER shifts opened by an upgraded till.
    # Non-cashier and pre-upgrade shifts deliberately keep this blank so a
    # manager/waiter can work on the same till and rolling upgrades remain
    # compatible. The conditional unique constraint below makes the non-empty
    # value the exclusive live cashier slot for that physical device.
    device_id = models.CharField(max_length=128, blank=True, default='')
    # Rollout eligibility for the reconciliation->SAFE lifecycle. Migration
    # 0048 leaves every already-ended historical shift false because its money
    # may already have reached treasury through legacy Inkassa and cannot be
    # linked safely. Upgraded ShiftService.start_shift explicitly sets true.
    # Fail-closed default=False is intentional: a pre-upgrade offline branch
    # can sync an old shift after rollout without this new field, and that row
    # must never become eligible merely because it arrived late.
    treasury_settlement_eligible = models.BooleanField(default=False)
    # Local close handshake. The Shift row intentionally syncs before its
    # child ShiftPaymentTotal/CashboxExpense rows; cloud reconciliation must
    # compare those children with this frozen manifest and refuse to post until
    # the complete bundle has arrived.
    settlement_manifest = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()
    SYNC_IMMUTABLE_FROM_BRANCH_AFTER_SET = frozenset({'settlement_manifest'})
    SYNC_CREATE_ONLY_FROM_BRANCH = frozenset({
        'treasury_settlement_eligible', 'device_id',
    })
    # Once the close handshake exists, the branch cannot move the economic
    # window/owner/totals or revert cloud COMPLETED back to ENDED. The first
    # close update is still accepted while the current manifest is empty.
    SYNC_IMMUTABLE_FROM_BRANCH_AFTER_MANIFEST = frozenset({
        'user', 'start_time', 'end_time', 'status', 'total_orders',
        'total_revenue', 'cash_collected',
    })

    class Meta:
        ordering = ['-start_time']
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=(
                    models.Q(is_deleted=False)
                    & models.Q(status='ACTIVE')
                    & models.Q(end_time__isnull=True)
                ),
                name='uniq_live_active_shift_per_user',
            ),
            models.UniqueConstraint(
                fields=['device_id'],
                condition=(
                    models.Q(is_deleted=False)
                    & models.Q(status='ACTIVE')
                    & models.Q(end_time__isnull=True)
                    & ~models.Q(device_id='')
                ),
                name='uniq_live_shift_per_device',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        data['shift_template_uuid'] = str(self.shift_template.uuid) if self.shift_template else None
        return data

    def __str__(self):
        return f"Shift: {self.user} ({self.start_time})"


class CashReconciliation(SyncMixin, models.Model):
    shift = models.OneToOneField(Shift, on_delete=models.CASCADE, related_name='reconciliation')
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    actual_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True, default='')
    reconciled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='reconciliations',
    )
    # Rollout boundary for the manager-confirmation treasury lifecycle. Legacy
    # reconciliations remain null because their proceeds may already have been
    # credited by the old Inkassa path; retrying one must never backfill and
    # double-credit SAFE. New reconciliations stamp this atomically with their
    # per-tender SHIFT_DEPOSIT rows (including a zero-total settlement).
    treasury_posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()
    _sync_append_only = True
    _sync_ingest_disabled = True
    SYNC_DENY_FROM_BRANCH = frozenset({'treasury_posted_at'})

    class Meta:
        ordering = ['-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['shift_uuid'] = str(self.shift.uuid) if self.shift else None
        data['reconciled_by_uuid'] = str(self.reconciled_by.uuid) if self.reconciled_by else None
        return data

    def __str__(self):
        return f"Reconciliation for {self.shift} (diff: {self.difference})"


class SyncQueueRecord(models.Model):
    """Durable queue for outbound sync records.

    Replaces the cache-backed queue so a process restart (LocMem) or a Redis
    crash before a flush no longer loses unsent records. Not a SyncMixin —
    this table is local-only bookkeeping and must never sync itself.
    """

    model_name = models.CharField(max_length=100, db_index=True)
    record_uuid = models.UUIDField(db_index=True)
    # Opaque identity for the exact payload currently stored in this queue row.
    # Every payload replacement rotates the token.  Push acknowledgements carry
    # the token they sent and may only delete/fail that same generation, so a
    # late response can never consume a newer edit that reused (model, uuid).
    generation = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    payload = models.JSONField()
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sync_queue_record'
        # One pending entry per (model, uuid). Re-queueing the same record
        # collapses on the unique constraint and updates the row in place.
        constraints = [
            models.UniqueConstraint(
                fields=['model_name', 'record_uuid'],
                name='uniq_sync_queue_model_uuid',
            ),
        ]
        ordering = ['created_at']

    def __str__(self):
        return f"SyncQueue<{self.model_name} {self.record_uuid}>"


class SyncState(models.Model):
    """Durable key/value for sync control state that must survive a process
    restart and a cache flush.

    Notably the pull CURSOR (`last_pull`): the cloud-clock `synced_at` frontier
    the pull loop resumes from. It used to live only in the cache-backed
    SyncStatus (24h TTL + per-process LocMem fallback), so a restart or a >24h
    offline window silently reset it and forced a full re-pull. The cursor is
    the source of truth for "what have I already pulled" and belongs in the DB.

    Not a SyncMixin — local bookkeeping, must never propagate.
    """

    key = models.CharField(max_length=64, primary_key=True)
    value = models.TextField(blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sync_state'

    def __str__(self):
        return f"SyncState<{self.key}={self.value}>"


class AuditLog(SyncMixin, models.Model):
    """Append-only record of sensitive admin actions.

    Written from the view layer where the actor (request.user) and the client
    IP are already known. Conflict-free under sync: each row is created once
    with sync_version=1 and never mutated, so the standard SyncMixin tiebreak
    naturally leaves it alone on the receiving side.
    """

    class Action(models.TextChoices):
        INKASSA_PERFORM = "INKASSA_PERFORM", "Inkassa performed"
        USER_CREATE = "USER_CREATE", "User created"
        USER_UPDATE = "USER_UPDATE", "User updated"
        USER_DELETE = "USER_DELETE", "User deleted"
        SHIFT_RECONCILE = "SHIFT_RECONCILE", "Shift reconciled"
        ORDER_CANCEL = "ORDER_CANCEL", "Order canceled"
        PRODUCT_PRICE_CHANGE = "PRODUCT_PRICE_CHANGE", "Product price changed"
        DISCOUNT_CREATE = "DISCOUNT_CREATE", "Discount created"
        DISCOUNT_UPDATE = "DISCOUNT_UPDATE", "Discount updated"
        DISCOUNT_DELETE = "DISCOUNT_DELETE", "Discount deleted"
        LOYALTY_REDEEM = "LOYALTY_REDEEM", "Loyalty stamps redeemed"
        TREASURY_TRANSFER = "TREASURY_TRANSFER", "Treasury transfer"
        TREASURY_EXPENSE = "TREASURY_EXPENSE", "Treasury expense"
        ORDER_PAYMENT_REPAIR = (
            "ORDER_PAYMENT_REPAIR", "Order payment repaired"
        )

    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_actions',
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    target_type = models.CharField(max_length=32, blank=True, default='', db_index=True)
    target_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-created_at']

    # One-way, append-only branch -> cloud evidence. The receive endpoint is
    # authenticated with the branch token and pins branch_id to that identity;
    # the cloud stores a UUID once and refuses later mutation. The change feed
    # excludes this model so one branch never receives another branch's audit
    # records (and a terminal cannot overwrite its own history by pulling).
    _sync_pull_disabled = True
    _sync_append_only = True

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['actor_uuid'] = str(self.actor.uuid) if self.actor else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        # Defense in depth on the pull/apply path. AuditLog is intentionally
        # never present in the cloud change feed; if a bad peer does include
        # one, a terminal still refuses to materialize it.
        return None, 'skipped'

    @classmethod
    def record(cls, *, actor, action, target_type='', target_id=None,
               metadata=None, ip_address=''):
        return cls.objects.create(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
            ip_address=ip_address,
        )

    def __str__(self):
        return f"AuditLog<{self.action} {self.target_type}#{self.target_id}>"


class IdempotencyKey(models.Model):
    """Local-only dedup record for retried write requests.

    Keyed by (scope, key). `scope` embeds the actor id and the endpoint so two
    clients can't accidentally replay each other's responses with the same
    header value. `response_status == 0` flags a still-in-flight claim and
    causes concurrent retries to fail fast with 409 instead of double-acting.

    Not a SyncMixin — like SyncQueueRecord this is per-branch bookkeeping and
    must never propagate.
    """

    scope = models.CharField(max_length=100, db_index=True)
    key = models.CharField(max_length=128, db_index=True)
    # Bind one client key to the exact request it first represented.  Resource
    # path lives in ``scope`` so different orders never collide; this digest
    # additionally rejects changing the query/body under the same key (for
    # example a second treasury transfer with a different amount).
    request_fingerprint = models.CharField(max_length=64, blank=True, default='')
    response_status = models.PositiveSmallIntegerField(default=0)
    response_body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'idempotency_key'
        constraints = [
            models.UniqueConstraint(
                fields=['scope', 'key'],
                name='uniq_idempotency_scope_key',
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"IdempotencyKey<{self.scope} {self.key[:12]}…>"


class DisplayIdCounter(models.Model):
    """Per-scope counter for Order.display_id allocation.

    `display_id` is the small kitchen-handoff number ("order #42 ready") and
    must be unique enough to be unambiguous on the line. Pre-fix, each
    surface (admin / cashier / waiter) read the latest order's display_id
    and added one — racy under concurrent creates, and the customer surface
    used `last+1` while the others used `(last % 100)+1`, so the same id
    could be assigned twice on the same day.

    The counter is locked via select_for_update inside the order-create
    transaction. One row per branch_id (default scope: 'default').

    Not a SyncMixin — counters are per-branch bookkeeping and must never
    propagate (each branch maintains its own kitchen-handoff numbering).
    """

    scope = models.CharField(max_length=64, primary_key=True)
    value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'display_id_counter'

    def __str__(self):
        return f"DisplayIdCounter<{self.scope}={self.value}>"


class ChefQueueCounter(models.Model):
    """Per-scope MONOTONIC counter for Order.chef_queue_number.

    Identical in shape to DisplayIdCounter but never wraps: the chef display
    needs an ever-increasing queue number so a busy line never sees the count
    jump back to #1 after #100 (which display_id does on purpose, to keep the
    cashier/receipt number short). Locked via select_for_update inside the
    order-create transaction. One row per branch_id (default scope: 'default').

    Not a SyncMixin — per-branch bookkeeping, must never propagate.
    """

    scope = models.CharField(max_length=64, primary_key=True)
    value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'chef_queue_counter'

    def __str__(self):
        return f"ChefQueueCounter<{self.scope}={self.value}>"


class SequenceCounter(models.Model):
    """Per-scope monotonic counter for human-readable document numbers
    (TRX / PO / TRF / CNT / PROD / RCV / CTR / BAT - YYYYMMDD - NNNN).

    Replaces the racy read-max-then-+1 in
    `base.services.sequence.generate_number`, which under concurrent
    creates produced duplicate numbers and tripped the unique constraint
    (e.g. `StockTransaction.transaction_number`) — aborting the sale's stock
    deduction. On Postgres the old read wasn't locked; on SQLite this counter
    plus the IMMEDIATE-transaction setting serialize allocation. Locked via
    select_for_update, exactly like DisplayIdCounter.

    One row per `prefix-date` scope. Seeded lazily from the current max so it
    never collides with numbers created before this counter existed.

    Not a SyncMixin — numbering is per-branch bookkeeping and must never
    propagate to sibling branches or the cloud.
    """

    scope = models.CharField(max_length=64, primary_key=True)
    value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sequence_counter'

    def __str__(self):
        return f"SequenceCounter<{self.scope}={self.value}>"


class OrderPayment(SyncMixin, models.Model):
    """One line of a (possibly split) order payment. A single sale can carry
    several rows — e.g. 60 000 Humo + 40 000 Cash, or two CASH lines. The
    Order keeps the rolled-up `payment_method` (a single method, or MIXED)."""
    order = models.ForeignKey(
        'base.Order', on_delete=models.CASCADE, related_name='payments',
    )
    method = models.CharField(max_length=10, choices=Order.PaymentMethod.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    # Logical checkout identity + deterministic position within its tender
    # split.  Payment row UUIDs are transport identities and can change when a
    # client reconstructs a queued request; this pair is the durable business
    # key.  Both remain NULL for pre-rollout clients.
    payment_action_id = models.UUIDField(null=True, blank=True, editable=False)
    line_index = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    # Payment evidence is append-only. A new UUID that carries the same logical
    # action/line is a replay, not another collection event.
    _sync_append_only = True
    SYNC_NATURAL_KEYS = ('order', 'payment_action_id', 'line_index')

    # The cloud is the collector of money rows; a peer cannot revise evidence.
    SYNC_WRITE_DENYLIST = frozenset({
        'amount', 'method', 'payment_action_id', 'line_index',
    })

    @classmethod
    def branch_sync_create_allowed(cls, *, uuid_val, values, resolved_fks):
        """Validate a new branch payment against the settled Order action.

        Old terminals omit both action fields and remain accepted while the
        Order itself is legacy (no action identity). Once a server Order owns
        an action, however, absent or different-action rows are stale/duplicate
        evidence and must be acknowledged without inserting another tender.
        """
        from decimal import Decimal, InvalidOperation

        order = resolved_fks.get('order')
        if order is None:
            return False
        action_id = values.get('payment_action_id')
        line_index = values.get('line_index')
        if (action_id is None) != (line_index is None):
            return False
        if (
            order.payment_action_id is not None
            and (
                action_id is None
                or str(action_id) != str(order.payment_action_id)
            )
        ):
            return False
        if values.get('method') not in {
            value for value, _label in Order.PaymentMethod.choices
            if value != Order.PaymentMethod.MIXED
        }:
            return False
        try:
            amount = Decimal(str(values.get('amount')))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return amount.is_finite() and amount > 0

    class Meta:
        db_table = 'order_payment'
        indexes = [models.Index(fields=['order', 'method'])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        payment_action_id__isnull=True,
                        line_index__isnull=True,
                    )
                    | models.Q(
                        payment_action_id__isnull=False,
                        line_index__isnull=False,
                    )
                ),
                name='order_payment_action_pair_complete',
            ),
            models.UniqueConstraint(
                fields=['order', 'payment_action_id', 'line_index'],
                condition=models.Q(
                    is_deleted=False,
                    payment_action_id__isnull=False,
                ),
                name='uniq_live_order_payment_action_line',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        """Apply each logical tender line once on cloud and branch receivers."""
        uuid_value = data.get('uuid')
        if uuid_value:
            existing = cls.objects.filter(uuid=uuid_value).first()
            if existing is not None:
                return existing, 'skipped'

        action_id = data.get('payment_action_id')
        line_index = data.get('line_index')
        if (action_id in (None, '')) != (line_index in (None, '')):
            return None, 'skipped'

        order = None
        order_uuid = data.get('order_uuid')
        if order_uuid:
            order = Order.objects.filter(uuid=order_uuid).first()
        if order is not None:
            if (
                order.payment_action_id is not None
                and (
                    action_id in (None, '')
                    or str(action_id) != str(order.payment_action_id)
                )
            ):
                return None, 'skipped'
            if action_id not in (None, '') and line_index not in (None, ''):
                logical = cls.objects.filter(
                    order=order,
                    payment_action_id=action_id,
                    line_index=line_index,
                    is_deleted=False,
                ).first()
                if logical is not None:
                    return logical, 'skipped'
        return super().from_sync_dict(data, branch_id=branch_id)

    def __str__(self):
        return f"OrderPayment<{self.method} {self.amount} on #{self.order_id}>"


class ExternalOrderPayment(SyncMixin, models.Model):
    """Immutable payment evidence collected outside a POS cash drawer.

    ``OrderPayment`` is till tender: its CASH rows may include customer change
    and participate in physical drawer reconciliation.  Courier/provider money
    has a different accounting meaning, so overloading that table makes a
    synced cloud collection look as if it entered the owning terminal's drawer.

    This write-once event is the cross-edition contract instead.  It is synced
    to the owning branch, can repair the protected ``Order.is_paid`` header,
    and participates in revenue/tender attribution, but it is *never* drawer
    cash.  Provider refunds remain separate append-only ``OrderRefund`` events;
    the positive collection is not deleted or rewritten.
    """

    class Source(models.TextChoices):
        COURIER = 'COURIER', 'Courier/provider collection'

    order = models.ForeignKey(
        'base.Order', on_delete=models.PROTECT,
        related_name='external_payments',
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    source_id = models.CharField(max_length=160)
    method = models.CharField(max_length=10, choices=Order.PaymentMethod.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    occurred_at = models.DateTimeField(db_index=True)

    objects = SyncManager()

    # UUID and the branch-scoped provider key both identify the same immutable
    # money event.  Exact pull/push replays are no-ops; no peer can revise it.
    _sync_append_only = True
    SYNC_NATURAL_KEYS = ('branch_id', 'source', 'source_id')

    @classmethod
    def branch_sync_create_allowed(cls, *, uuid_val, values, resolved_fks):
        """Accept only a complete concrete event from its owning branch.

        A branch is already allowed to originate ordinary OrderPayment rows;
        this guard gives courier/mobile collections the same authority without
        allowing arbitrary sources, MIXED pseudo-lines, zero money, or an event
        detached from a branch-owned Order.
        """
        from decimal import Decimal, InvalidOperation

        order = resolved_fks.get('order')
        if order is None:
            return False
        if values.get('source') != cls.Source.COURIER:
            return False
        if not str(values.get('source_id') or '').strip():
            return False
        if values.get('method') not in {
            value for value, _label in Order.PaymentMethod.choices
            if value != Order.PaymentMethod.MIXED
        }:
            return False
        try:
            amount = Decimal(str(values.get('amount')))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return amount.is_finite() and amount > 0 and bool(values.get('occurred_at'))

    class Meta:
        db_table = 'external_order_payment'
        indexes = [
            models.Index(
                fields=['order', 'method'], name='extpay_order_method_idx',
            ),
            models.Index(
                fields=['branch_id', 'occurred_at'],
                name='extpay_branch_time_idx',
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['branch_id', 'source', 'source_id'],
                name='uniq_external_payment_source_event',
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='external_payment_amount_positive',
            ),
            models.CheckConstraint(
                condition=models.Q(source='COURIER'),
                name='external_payment_source_known',
            ),
            models.CheckConstraint(
                condition=models.Q(method__in=[
                    Order.PaymentMethod.CASH,
                    Order.PaymentMethod.UZCARD,
                    Order.PaymentMethod.HUMO,
                    Order.PaymentMethod.CARD,
                    Order.PaymentMethod.PAYME,
                ]),
                name='external_payment_method_concrete',
            ),
            models.CheckConstraint(
                condition=~models.Q(source_id=''),
                name='external_payment_source_id_required',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        """Materialize once; every UUID/natural-key replay is a no-op.

        CloudReceiver already enforces ``_sync_append_only`` for branch pushes,
        but terminal pulls call ``SyncMixin.from_sync_dict`` directly. Keep the
        invariant at the model boundary so a forged higher sync_version cannot
        revise an event after the first accepted insert.
        """
        uuid_value = data.get('uuid')
        if uuid_value:
            existing = cls.objects.filter(uuid=uuid_value).first()
            if existing is not None:
                return existing, 'skipped'
        incoming_branch = data.get('branch_id') or branch_id or ''
        source = data.get('source')
        source_id = data.get('source_id')
        if incoming_branch and source and source_id:
            existing = cls.objects.filter(
                branch_id=incoming_branch,
                source=source,
                source_id=source_id,
            ).first()
            if existing is not None:
                return existing, 'skipped'
        return super().from_sync_dict(data, branch_id=branch_id)

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get('_syncing', False):
            raise ValueError('ExternalOrderPayment events are immutable')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('ExternalOrderPayment events cannot be deleted')

    def hard_delete(self, *args, **kwargs):
        raise ValueError('ExternalOrderPayment events cannot be deleted')

    @property
    def affects_drawer(self):
        return False

    def __str__(self):
        return (
            f"ExternalOrderPayment<{self.source}:{self.source_id} "
            f"{self.method} {self.amount} on #{self.order_id}>"
        )


class OrderRefund(SyncMixin, models.Model):
    """Immutable settlement-reversal event for a paid order.

    Cancelling an order is an operational state transition; it must not erase
    the original sale or mutate its ``paid_at``/payment rows.  A refund is a
    separate money event dated when the money was returned.  The tender split
    is frozen here so later payment-provider changes cannot rewrite historical
    cash/card/Payme reconciliation.

    Cashier-initiated refunds carry the ACTIVE shift that performed them.
    Provider-origin refunds are intentionally shiftless: courier money never
    entered a till. ``source`` + ``source_id`` make every external event
    independently idempotent while preserving an append-only history.
    """
    REGISTER_COMMAND_MARKER = '[ALPHAPOS_REFUND_REGISTER_COMMAND_V1]'

    class Source(models.TextChoices):
        ORDER_CANCEL = 'ORDER_CANCEL', 'Order cancellation'
        COURIER_PAYMENT = 'COURIER_PAYMENT', 'Courier/provider payment'

    order = models.ForeignKey(
        'base.Order', on_delete=models.PROTECT, related_name='refunds',
        related_query_name='refund',
    )
    shift = models.ForeignKey(
        'base.Shift', on_delete=models.PROTECT, null=True, blank=True,
        related_name='order_refunds',
    )
    cashier = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='order_refunds',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cash_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Subset of cash_amount that originally entered a POS drawer. Courier cash
    # remains cash for tender analytics but must never debit CashRegister.
    drawer_cash_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )
    card_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payme_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unknown_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Acquirer detail (UZCARD/HUMO/CARD), frozen at the refund event.  The
    # collapsed card_amount remains the presentation/reporting bucket.
    card_detail = models.JSONField(default=dict, blank=True)
    refunded_at = models.DateTimeField(db_index=True)
    # Database-local receipt cursor. A late synced refund keeps its economic
    # refunded_at while entering the next unclosed Inkassa accounting batch.
    accounting_recorded_at = models.DateTimeField(
        db_default=Now(), editable=False,
    )
    # True only for a cloud-issued instruction. A locally-created refund
    # debits its drawer in the same transaction and therefore leaves this False.
    register_command = models.BooleanField(default=False)
    source = models.CharField(max_length=32, choices=Source.choices)
    source_id = models.CharField(max_length=160)
    reason = models.CharField(max_length=255, blank=True, default='')

    objects = SyncManager()

    SYNC_NATURAL_KEYS = ('source', 'source_id')
    _sync_append_only = True

    # A branch owns its settlement and the cloud aggregates it.  A later cloud
    # pull must never rewrite a refund already recorded by this till; cloud
    # receivers still accept these fields because direction-aware ingest uses
    # SYNC_DENY_FROM_BRANCH on the cloud side.
    SYNC_WRITE_DENYLIST = frozenset({
        'amount', 'cash_amount', 'drawer_cash_amount', 'card_amount', 'payme_amount',
        'unknown_amount', 'card_detail', 'refunded_at', 'register_command',
        'source', 'source_id', 'reason', 'accounting_recorded_at',
    })
    SYNC_DENY_FROM_BRANCH = frozenset({
        'register_command', 'accounting_recorded_at',
    })

    class Meta:
        db_table = 'order_refund'
        indexes = [
            models.Index(fields=['refunded_at', 'cashier']),
            models.Index(fields=['branch_id', 'refunded_at']),
            models.Index(
                fields=['branch_id', 'accounting_recorded_at'],
                name='refund_branch_acct_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(amount__gte=0)
                    & models.Q(cash_amount__gte=0)
                    & models.Q(drawer_cash_amount__gte=0)
                    & models.Q(card_amount__gte=0)
                    & models.Q(payme_amount__gte=0)
                    & models.Q(unknown_amount__gte=0)
                ),
                name='order_refund_amounts_nonnegative',
            ),
            models.CheckConstraint(
                condition=models.Q(drawer_cash_amount__lte=models.F('cash_amount')),
                name='order_refund_drawer_cash_lte_cash',
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount=(
                        models.F('cash_amount') + models.F('card_amount')
                        + models.F('payme_amount') + models.F('unknown_amount')
                    )
                ),
                name='order_refund_tenders_sum_amount',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(shift__isnull=True, cashier__isnull=True)
                    | models.Q(shift__isnull=False, cashier__isnull=False)
                ),
                name='order_refund_shift_cashier_pair',
            ),
            models.CheckConstraint(
                condition=~models.Q(source_id=''),
                name='order_refund_source_id_required',
            ),
            models.UniqueConstraint(
                fields=['source', 'source_id'],
                name='uniq_order_refund_source_event',
            ),
            models.UniqueConstraint(
                fields=['order'],
                condition=models.Q(source='ORDER_CANCEL'),
                name='uniq_order_cancel_refund',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        data['shift_uuid'] = str(self.shift.uuid) if self.shift else None
        data['cashier_uuid'] = str(self.cashier.uuid) if self.cashier else None
        data.pop('accounting_recorded_at', None)
        return data

    @classmethod
    def command_reason(cls, reason=''):
        return f'{cls.REGISTER_COMMAND_MARKER}\n{str(reason or "")}'[:255]

    @classmethod
    def visible_reason(cls, reason=''):
        text = str(reason or '')
        prefix = f'{cls.REGISTER_COMMAND_MARKER}\n'
        return text[len(prefix):] if text.startswith(prefix) else text

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        """Materialize once; never let a peer revise or tombstone a ledger row."""
        data = data.copy()
        data.pop('accounting_recorded_at', None)
        if data.get('is_deleted'):
            return None, 'skipped'
        existing = cls.objects.filter(uuid=data.get('uuid')).first()
        if existing is not None:
            if existing.register_command:
                applied = Inkassa._apply_pending_register_commands(existing.branch_id)
                if not applied:
                    return existing, 'deferred'
            return existing, 'skipped'
        source = data.get('source')
        source_id = data.get('source_id')
        if source and source_id:
            existing = cls.objects.filter(
                source=source, source_id=source_id,
            ).first()
            if existing is not None:
                if existing.register_command:
                    applied = Inkassa._apply_pending_register_commands(
                        existing.branch_id,
                    )
                    if not applied:
                        return existing, 'deferred'
                return existing, 'skipped'
        instance, action = super().from_sync_dict(data, branch_id=branch_id)
        if instance is not None and instance.register_command:
            applied = Inkassa._apply_pending_register_commands(instance.branch_id)
            if not applied:
                return instance, 'deferred'
        return instance, action

    def save(self, *args, **kwargs):
        # Receiver upserts must be able to materialize a trusted peer event;
        # application code may only INSERT.  Corrections are new accounting
        # events, never edits to an existing reversal.
        syncing = kwargs.get('_syncing', False)
        if self.pk and not syncing:
            raise TypeError('OrderRefund is append-only and cannot be edited')
        if self.pk:
            return super().save(*args, **kwargs)

        using = kwargs.get('using') or self._state.db or 'default'
        with transaction.atomic(using=using):
            from base.services.accounting_cursor import lock_branch_accounting

            branch = self.branch_id
            if not branch and self.order_id:
                branch = self.order.branch_id
            register = lock_branch_accounting(branch or None)
            if not self.branch_id:
                self.branch_id = register.branch_id
            # The database default is evaluated only after the serialization
            # lock above has been acquired.  Keeping it in the DDL also lets
            # older cross-app data migrations insert ledger rows safely on a
            # brand-new database even though their historical model state does
            # not know this newer column exists.
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('OrderRefund is append-only and cannot be deleted')

    def hard_delete(self, *args, **kwargs):
        raise TypeError('OrderRefund is append-only and cannot be deleted')

    def __str__(self):
        return f"OrderRefund<{self.source}:{self.source_id} {self.amount} on order #{self.order_id}>"


class PaymentMethodConfig(models.Model):
    """Branding/catalog for the cashier payment screen: which methods show,
    their label, inline SVG icon and accent color. Seeded with the four
    built-ins; vendor-editable in admin. Plain (non-synced) per-install config
    — the frontend caches it per-PC after login."""
    code = models.CharField(
        max_length=10, unique=True, choices=Order.PaymentMethod.choices,
    )
    label = models.CharField(max_length=40)
    icon = models.TextField(blank=True, default='', help_text='Inline SVG (24x24, currentColor)')
    color = models.CharField(max_length=9, default='#3b82f6')
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_method_config'
        ordering = ['sort_order', 'code']

    def __str__(self):
        return f"PaymentMethodConfig<{self.code}>"


class RolePermission(models.Model):
    """Per-role default permission set, edited by Settings → Roles. Enforcement
    is per-user (User.permissions); this is the role template the editor manages
    and new users inherit. Plain (non-synced) config."""
    role = models.CharField(max_length=20, unique=True, choices=User.RoleChoices.choices)
    permissions = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'role_permission'

    def __str__(self):
        return f"RolePermission<{self.role}>"
