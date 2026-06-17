import uuid
from decimal import Decimal
from django.db import models, transaction
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

    def save(self, *args, **kwargs):
        syncing = kwargs.pop('_syncing', False)
        if not syncing:
            if not self.branch_id and hasattr(settings, 'BRANCH_ID'):
                self.branch_id = settings.BRANCH_ID
            if self.pk:
                self.sync_version += 1
            mode = getattr(settings, 'DEPLOYMENT_MODE', 'local')
            update_fields = kwargs.get('update_fields')
            content_changed = update_fields is None or any(
                f not in ['synced_at', 'sync_version'] for f in update_fields
            )
            if content_changed:
                if mode == 'local':
                    # Branch: mark pending so the push worker sends it to the hub.
                    self.synced_at = None
                elif mode == 'cloud':
                    # Hub: publish records it originates with a timestamp so the
                    # /changes cursor hands them to branches on pull. (Records
                    # RECEIVED from a branch run with _syncing=True and skip this.)
                    from django.utils import timezone
                    self.synced_at = timezone.now()
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
        super().save(*args, **kwargs)
        if (not syncing and not self._sync_local_only
                and self.synced_at is None and self._is_sync_on_save()):
            # Defer queueing until the surrounding transaction commits so a
            # rollback doesn't leave an orphan UUID in the sync queue that
            # the cloud cannot resolve. on_commit fires immediately when no
            # transaction is open.
            transaction.on_commit(self._queue_for_sync)

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
            super().delete(*args, **kwargs)
        else:
            self.is_deleted = True
            self.save(update_fields=['is_deleted', 'synced_at', 'sync_version'])

    def hard_delete(self):
        # Capture identity for a tombstone before we delete the row, then
        # enqueue a soft-delete sync record on commit so peers also remove
        # the record. Without this, hard deletes on one branch never
        # propagate and leave dangling FK references on others.
        if self._is_sync_on_save() and not self._sync_local_only and self.pk:
            try:
                from base.services.sync.service import SyncService
                model_name = self.__class__.__name__.lower()
                tombstone = self.to_sync_dict()
                tombstone['is_deleted'] = True
                tombstone['sync_version'] = (self.sync_version or 0) + 1
                uuid_val = str(self.uuid)
                transaction.on_commit(
                    lambda: SyncService.queue_tombstone(model_name, uuid_val, tombstone)
                )
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to queue tombstone for {self.__class__.__name__} pk={self.pk}",
                    exc_info=True,
                )
        super().delete()

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
    # On CREATE a denied field that is required (NOT NULL, no default) is still
    # written: the row cannot materialize otherwise, and a row we don't yet hold
    # locally has no value to protect. Protection is meaningful only on UPDATE.
    SYNC_WRITE_DENYLIST = frozenset()
    SYNC_DENY_FROM_BRANCH = frozenset()

    # Natural keys that uniquely identify a record independent of its uuid.
    # When an incoming sync record's uuid isn't found locally but another row
    # already owns the same natural-key value (e.g. User.email is unique), we
    # reconcile onto that row — adopting the incoming uuid — instead of blindly
    # INSERTing a duplicate that trips the DB unique constraint and gets
    # silently dropped by _apply_records (permanent loss of a server-created
    # user). Empty by default.
    SYNC_NATURAL_KEYS = ()

    @classmethod
    def _find_by_natural_key(cls, data, resolved_fks=None):
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
            return frozenset(getattr(cls, 'SYNC_DENY_FROM_BRANCH', frozenset()))
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
        cleaned = {}
        for key, value in data.items():
            if key in denied:
                if creating and cls._sync_required_no_default(key):
                    # Required column on a brand-new row — keep the origin value
                    # so the record can be inserted; nothing local to protect.
                    cleaned[key] = value
                    continue
                logger.warning(
                    'sync ingest: dropping denylisted field %s on %s (mode=%s)',
                    key, cls.__name__, mode or getattr(settings, 'DEPLOYMENT_MODE', 'local'),
                )
                continue
            cleaned[key] = value
        return cleaned

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime
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
            uuid_value = data.pop(uuid_field)
            if not uuid_value:
                continue
            try:
                related = apps.get_model(app_label, model_name).objects.filter(
                    uuid=uuid_value,
                ).first()
            except Exception:
                related = None
            if related is not None:
                resolved_fks[fk_field] = related
            else:
                # Don't materialize a row with a missing *required* FK — the
                # parent UUID hasn't synced yet. Skip; the next pull cycle
                # re-delivers once the parent has landed.
                try:
                    if not cls._meta.get_field(fk_field).null:
                        import logging
                        logging.getLogger(__name__).warning(
                            'sync ingest: unresolved required FK %s=%s on %s; '
                            'deferring record %s for retry',
                            fk_field, uuid_value, cls.__name__, uuid_val,
                        )
                        # 'deferred' (not 'skipped'): the puller retries these
                        # after the rest of the pull lands the parent, so a child
                        # pulled before its parent isn't lost when the cursor
                        # advances past it.
                        return None, 'deferred'
                except Exception:
                    pass

        # Source-of-truth `updated_at`. We need to preserve this across the
        # save(), because every SyncMixin model declares updated_at with
        # auto_now=True — Django would otherwise overwrite the incoming
        # timestamp with the receiver's local clock at save-time, silently
        # breaking the equal-version tiebreaker on the next round.
        incoming_updated = data.pop('updated_at', None)
        if isinstance(incoming_updated, str):
            incoming_updated = parse_datetime(incoming_updated)

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
            for key, value in cls._strip_sync_denied(data, creating=False).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            for fk_field, related in resolved_fks.items():
                setattr(instance, fk_field, related)
            instance.sync_version = sync_version
            instance.is_deleted = is_deleted
            instance.synced_at = timezone.now()
            instance.save(_syncing=True)
            if incoming_updated and hasattr(instance, 'updated_at'):
                # .update() bypasses auto_now so the source-of-truth
                # timestamp survives. Reload onto the instance for callers.
                cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                instance.updated_at = incoming_updated
            return instance, 'updated'
        except cls.DoesNotExist:
            # uuid not present locally. Before INSERTing, check whether a
            # different local row already owns one of this model's natural keys
            # (e.g. a server-created user whose email matches an existing local
            # user). If so, reconcile onto that row — converging on the incoming
            # uuid — rather than INSERTing a duplicate that would raise
            # IntegrityError and be silently dropped, never to retry.
            natural = cls._find_by_natural_key(data, resolved_fks)
            if natural is not None:
                instance = natural
                instance.uuid = uuid_val
                # Reconcile onto an existing row → an UPDATE: protect denied
                # fields just like the version-matched update branch.
                for key, value in cls._strip_sync_denied(data, creating=False).items():
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                for fk_field, related in resolved_fks.items():
                    setattr(instance, fk_field, related)
                instance.sync_version = sync_version
                instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                instance.branch_id = incoming_branch or instance.branch_id or ''
                instance.save(_syncing=True)
                if incoming_updated and hasattr(instance, 'updated_at'):
                    cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                    instance.updated_at = incoming_updated
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
            if incoming_updated and hasattr(instance, 'updated_at'):
                cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                instance.updated_at = incoming_updated
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
    class RoleChoices(models.TextChoices):
        USER = "USER", "User"
        ADMIN = "ADMIN", "Admin"
        CASHIER = "CASHIER", "Cashier"
        # Monoblock-level manager: logs in on the POS next to cashiers (NOT in
        # the admin dashboard like ADMIN), but with elevated in-app access
        # (settings, etc.). Gated server-side via role_required('MANAGER').
        MANAGER = "MANAGER", "Manager"
        WAITER = "WAITER", "Waiter"
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
        # The base implementation only setattrs keys the model has, so
        # parent_category_uuid (a synthetic FK pointer) is silently dropped on
        # the pull path — flattening the category tree on pulled branches.
        # Resolve it explicitly here, then defer to the base for everything
        # else (updated_at preservation, version tiebreaker, branch checks).
        from django.utils import timezone

        data = data.copy()
        parent_uuid = data.pop('parent_category_uuid', None)
        instance, action = super().from_sync_dict(data, branch_id=branch_id)
        # Only touch the parent link when the row was actually written —
        # a 'skipped' result means the incoming version lost the tiebreaker,
        # so its parent must not overwrite the newer local value.
        if instance is None or action not in ('created', 'updated'):
            return instance, action

        parent = None
        if parent_uuid and parent_uuid != str(instance.uuid):  # never self-parent
            parent = cls.objects.filter(uuid=parent_uuid).first()
        if instance.parent_id != (parent.id if parent else None):
            instance.parent = parent
            cls.objects.filter(pk=instance.pk).update(
                parent=parent, synced_at=timezone.now(),
            )
        return instance, action

    def __str__(self):
        return self.name


class Product(SyncMixin, models.Model):
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

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        data = data.copy()
        category_uuid = data.pop('category_uuid', None)
        uuid_val = data.pop('uuid')
        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        incoming_branch = data.pop('branch_id', branch_id)

        # Preserve the source-of-truth updated_at across save(). updated_at is
        # auto_now=True, so a plain setattr+save would overwrite it with the
        # receiver's local clock and silently break the equal-version
        # tiebreaker in _should_replace on the next sync round.
        incoming_updated = data.pop('updated_at', None)
        if isinstance(incoming_updated, str):
            incoming_updated = parse_datetime(incoming_updated)

        category = None
        if category_uuid:
            try:
                category = Category.objects.get(uuid=category_uuid)
            except Category.DoesNotExist:
                pass

        try:
            instance = cls.objects.get(uuid=uuid_val)
            if cls._should_replace(
                instance, sync_version,
                {**data, 'updated_at': incoming_updated}, incoming_branch,
            ):
                for key, value in cls._strip_sync_denied(data, creating=False).items():
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                if category:
                    instance.category = category
                instance.sync_version = sync_version
                instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                instance.save(_syncing=True)
                if incoming_updated:
                    cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                    instance.updated_at = incoming_updated
            return instance, 'updated'
        except cls.DoesNotExist:
            instance = cls(
                uuid=uuid_val,
                sync_version=sync_version,
                is_deleted=is_deleted,
                branch_id=incoming_branch or '',
                synced_at=timezone.now(),
                category=category,
            )
            for key, value in cls._strip_sync_denied(data, creating=True).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            instance.save(_syncing=True)
            if incoming_updated:
                cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                instance.updated_at = incoming_updated
            return instance, 'created'

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
        if not phone:
            return ''
        digits = ''.join(ch for ch in str(phone) if ch.isdigit())
        if len(digits) == 9:            # bare national number -> add the UZ code
            digits = '998' + digits
        return digits

    @classmethod
    def resolve(cls, phone=None, telegram_id=None, name=None):
        """Find-or-create the ONE master client for this person, converging the
        in-store (phone) and Telegram (telegram_id) identities onto a single row.

        Match priority: telegram_id, then exact phone, then normalized phone. On a
        match we backfill whichever key/name is missing (never clobbering existing
        data) so the next lookup from either channel hits the same row. This is the
        join that lets a walk-in's in-store orders + a bot login share one history.
        Returns (customer, created)."""
        name = (name or '').strip()[:120]
        phone = (str(phone).strip()[:20] if phone else '')
        norm = cls.normalize_phone(phone)
        qs = cls.objects.filter(is_deleted=False)

        # Phone is the cross-channel key, so match it FIRST: a Telegram login that
        # shares its number converges onto the in-store walk-in row (created by
        # phone on the desktop) rather than spawning a second telegram-only row.
        customer = None
        if phone:
            customer = qs.filter(phone_number=phone).order_by('id').first()    # fast exact (indexed)
        if customer is None and norm:
            # Normalized fallback: a stored variant of the same number ('+998…', spaces).
            for cid, cphone in qs.exclude(phone_number='').values_list('id', 'phone_number'):
                if cls.normalize_phone(cphone) == norm:
                    customer = cls.objects.get(id=cid)
                    break
        if customer is None and telegram_id:
            customer = qs.filter(telegram_id=telegram_id).order_by('id').first()

        if customer is None:
            return cls.objects.create(
                name=name, phone_number=phone, telegram_id=telegram_id or None), True

        changed = False
        if telegram_id and not customer.telegram_id:
            customer.telegram_id = telegram_id; changed = True
        if phone and not customer.phone_number:
            customer.phone_number = phone; changed = True
        if name and not customer.name:
            customer.name = name; changed = True
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

    order_type = models.CharField(
        max_length=10,
        choices=OrderType.choices,
        default=OrderType.HALL,
    )

    phone_number = models.CharField(max_length=20, null=True, blank=True)
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
        PAYME = "PAYME", "Payme"
        # Set on the Order when a single sale is split across >1 distinct method.
        # The per-line breakdown lives in OrderPayment rows.
        MIXED = "MIXED", "Mixed"

    is_paid = models.BooleanField(default=False, db_index=True)
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
    # Indexed: dashboard/today, forecast/tomorrow, menu-engineering,
    # shift_performance, 1C export, and the Telegram /status command all
    # filter by created_at range; without the index every analytics call
    # is a heap scan on a constantly-growing table.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    objects = SyncManager()

    # Refuse sync ingestion of payment / total fields. The cloud is the
    # collector of these; a peer cannot dictate "this order is paid for
    # 99999". Field-level guard, not row-level — the rest of the order
    # (status transitions, item changes) still syncs normally.
    SYNC_WRITE_DENYLIST = frozenset({
        'is_paid', 'payment_method', 'total_amount', 'subtotal',
        'discount_amount', 'discount_percent', 'paid_at',
    })

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
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        data['cashier_uuid'] = str(self.cashier.uuid) if self.cashier else None
        data['delivery_person_uuid'] = str(self.delivery_person.uuid) if self.delivery_person else None
        data['place_uuid'] = str(self.place.uuid) if self.place else None
        data['table_uuid'] = str(self.table.uuid) if self.table else None
        data['customer_uuid'] = str(self.customer.uuid) if self.customer else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone
        from django.utils.dateparse import parse_datetime

        data = data.copy()
        user_uuid = data.pop('user_uuid', None)
        cashier_uuid = data.pop('cashier_uuid', None)
        delivery_person_uuid = data.pop('delivery_person_uuid', None)
        customer_uuid = data.pop('customer_uuid', None)
        uuid_val = data.pop('uuid')
        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        incoming_branch = data.pop('branch_id', branch_id)

        # Preserve the source-of-truth updated_at across save() (auto_now would
        # overwrite it with the local clock and break the equal-version
        # tiebreaker — see the base from_sync_dict for the full rationale).
        incoming_updated = data.pop('updated_at', None)
        if isinstance(incoming_updated, str):
            incoming_updated = parse_datetime(incoming_updated)

        user = None
        cashier = None
        delivery_person = None

        if user_uuid:
            try:
                user = User.objects.get(uuid=user_uuid)
            except User.DoesNotExist:
                user = None

        # A uuid that's present but not yet synced locally must NOT overwrite an
        # existing attribution link to NULL on update — that silently
        # de-attributes the order and corrupts cashier/shift stats. Track
        # resolvability so the update branch can skip the overwrite.
        cashier_unresolved = False
        if cashier_uuid:
            try:
                cashier = User.objects.get(uuid=cashier_uuid)
            except User.DoesNotExist:
                cashier_unresolved = True

        delivery_unresolved = False
        if delivery_person_uuid:
            try:
                delivery_person = DeliveryPerson.objects.get(uuid=delivery_person_uuid)
            except DeliveryPerson.DoesNotExist:
                delivery_unresolved = True

        # Optional client link — same soft-FK rule as cashier/delivery_person: a
        # uuid not yet synced locally must not wipe an existing link on update.
        customer = None
        customer_unresolved = False
        if customer_uuid:
            try:
                customer = Customer.objects.get(uuid=customer_uuid)
            except Customer.DoesNotExist:
                customer_unresolved = True

        if not user:
            # Required FK (Order.user) not present yet — defer for retry after
            # the rest of the pull lands the user, instead of erroring it away.
            return None, 'deferred'

        try:
            from django.db import transaction
            base_qs = cls.objects
            if transaction.get_connection().in_atomic_block:
                base_qs = cls.objects.select_for_update()
            instance = base_qs.get(uuid=uuid_val)
            if not cls._should_replace(
                instance, sync_version,
                {**data, 'updated_at': incoming_updated}, incoming_branch,
            ):
                # Stale/older payload — report the skip honestly so pull stats
                # don't over-count it as an applied update.
                return instance, 'skipped'
            # A locally-tombstoned order is terminal — never resurrect it from a
            # stale pre-delete payload.
            if instance.is_deleted and not is_deleted:
                return instance, 'skipped'
            for key, value in cls._strip_sync_denied(data, creating=False).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            instance.user = user
            # Don't wipe an existing attribution link when the incoming uuid
            # simply hasn't synced locally yet (see resolution above).
            if not cashier_unresolved:
                instance.cashier = cashier
            if not delivery_unresolved:
                instance.delivery_person = delivery_person
            if not customer_unresolved:
                instance.customer = customer
            instance.sync_version = sync_version
            instance.is_deleted = is_deleted
            instance.synced_at = timezone.now()
            instance.save(_syncing=True)
            if incoming_updated:
                cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                instance.updated_at = incoming_updated
            return instance, 'updated'
        except cls.DoesNotExist:
            instance = cls(
                uuid=uuid_val,
                sync_version=sync_version,
                is_deleted=is_deleted,
                branch_id=incoming_branch or '',
                synced_at=timezone.now(),
                user=user,
                cashier=cashier,
                delivery_person=delivery_person,
                customer=customer,
            )
            for key, value in cls._strip_sync_denied(data, creating=True).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            instance.save(_syncing=True)
            if incoming_updated:
                cls.objects.filter(pk=instance.pk).update(updated_at=incoming_updated)
                instance.updated_at = incoming_updated
            return instance, 'created'

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

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone

        data = data.copy()
        order_uuid = data.pop('order_uuid', None)
        product_uuid = data.pop('product_uuid', None)
        uuid_val = data.pop('uuid')
        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        incoming_branch = data.pop('branch_id', branch_id)
        data = cls._strip_sync_denied(data)

        order = None
        product = None

        if order_uuid:
            try:
                order = Order.objects.get(uuid=order_uuid)
            except Order.DoesNotExist:
                order = None

        if product_uuid:
            try:
                product = Product.objects.get(uuid=product_uuid)
            except Product.DoesNotExist:
                pass

        if not order:
            # Required FK (OrderItem.order) not present yet — defer for retry.
            return None, 'deferred'

        try:
            instance = cls.objects.get(uuid=uuid_val)
            if cls._should_replace(instance, sync_version, data, incoming_branch):
                for key, value in data.items():
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                instance.order = order
                if product:
                    instance.product = product
                instance.sync_version = sync_version
                instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                instance.save(_syncing=True)
            return instance, 'updated'
        except cls.DoesNotExist:
            instance = cls(
                uuid=uuid_val,
                sync_version=sync_version,
                is_deleted=is_deleted,
                branch_id=incoming_branch or '',
                synced_at=timezone.now(),
                order=order,
                product=product,
            )
            for key, value in data.items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            instance.save(_syncing=True)
            return instance, 'created'

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class CashRegister(SyncMixin, models.Model):
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_updated = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        db_table = 'cash_register'

    def __str__(self):
        return f"Cash Register: {self.current_balance}"


class Inkassa(SyncMixin, models.Model):
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
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    # Inkassa is a cash-history record. The amounts, balances, and
    # collected-revenue numbers must never be set from a peer push — they
    # are only ever computed locally at performance time. Sync should
    # propagate the *existence* of an Inkassa event, not let a peer dictate
    # its financial figures.
    SYNC_WRITE_DENYLIST = frozenset({
        'amount', 'balance_before', 'balance_after', 'total_revenue',
    })

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['cashier_uuid'] = str(self.cashier.uuid) if self.cashier else None
        return data

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        from django.utils import timezone

        data = data.copy()
        cashier_uuid = data.pop('cashier_uuid', None)
        uuid_val = data.pop('uuid')
        sync_version = data.pop('sync_version', 1)
        is_deleted = data.pop('is_deleted', False)
        incoming_branch = data.pop('branch_id', branch_id)

        cashier = None
        if cashier_uuid:
            try:
                cashier = User.objects.get(uuid=cashier_uuid)
            except User.DoesNotExist:
                pass

        try:
            instance = cls.objects.get(uuid=uuid_val)
            if cls._should_replace(instance, sync_version, data, incoming_branch):
                for key, value in cls._strip_sync_denied(data, creating=False).items():
                    if hasattr(instance, key):
                        setattr(instance, key, value)
                instance.cashier = cashier
                instance.sync_version = sync_version
                instance.is_deleted = is_deleted
                instance.synced_at = timezone.now()
                instance.save(_syncing=True)
            return instance, 'updated'
        except cls.DoesNotExist:
            instance = cls(
                uuid=uuid_val,
                sync_version=sync_version,
                is_deleted=is_deleted,
                branch_id=incoming_branch or '',
                synced_at=timezone.now(),
                cashier=cashier,
            )
            for key, value in cls._strip_sync_denied(data, creating=True).items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            instance.save(_syncing=True)
            return instance, 'created'

    def __str__(self):
        return f"Inkassa #{self.id} - {self.amount} on {self.created_at.strftime('%Y-%m-%d %H:%M')}"


class TreasuryAccount(SyncMixin, models.Model):
    """A money pot the business holds outside the till drawer.

    SAFE = physical cash moved out of the registers by inkassa.
    BANK = electronic money (card / Payme settlements).
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

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.type} {self.delta} ({self.account_id})"


class AppSettings(models.Model):
    hr_enabled = models.BooleanField(default=False)
    waiter_enabled = models.BooleanField(default=False)
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-start_time']

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
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

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

    # AuditLog is push-only to the cloud collector. Branch peers must not
    # be able to materialize forged entries via /api/sync/receive (e.g.,
    # claiming the victim's cashier_id performed an inkassa pull). Refuse
    # inbound writes entirely; the receiver-side handler short-circuits on
    # `_sync_ingest_disabled`.
    _sync_ingest_disabled = True

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        # No-op: refuse to materialize AuditLog rows from peer payloads.
        # Cloud-side collectors must construct AuditLog directly from the
        # local request context (actor=request.user, ip from REMOTE_ADDR).
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
    (TRX / PO / TRF / CNT / PROD / RCV - YYYYMMDD - NNNN).

    Replaces the racy read-max-then-+1 in
    `stock.services.base_service.generate_number`, which under concurrent
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
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    # The cloud is the collector of money rows; a peer cannot invent payments.
    SYNC_WRITE_DENYLIST = frozenset({'amount', 'method'})

    class Meta:
        db_table = 'order_payment'
        indexes = [models.Index(fields=['order', 'method'])]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        return data

    def __str__(self):
        return f"OrderPayment<{self.method} {self.amount} on #{self.order_id}>"


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
