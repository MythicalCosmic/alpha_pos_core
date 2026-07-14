class BaseRepository:
    model = None

    @classmethod
    def get_by_id(cls, pk):
        try:
            return cls.model.objects.get(pk=pk)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_all(cls):
        return cls.model.objects.all()

    @classmethod
    def create(cls, **kwargs):
        return cls.model.objects.create(**kwargs)

    @classmethod
    def update(cls, instance, **kwargs):
        for key, value in kwargs.items():
            setattr(instance, key, value)
        instance.save()
        return instance

    @classmethod
    def delete(cls, instance):
        instance.delete()

    @classmethod
    def filter(cls, **kwargs):
        return cls.model.objects.filter(**kwargs)

    @classmethod
    def count(cls, **kwargs):
        return cls.model.objects.filter(**kwargs).count()

    @classmethod
    def exists(cls, **kwargs):
        return cls.model.objects.filter(**kwargs).exists()

    @classmethod
    def first(cls, **kwargs):
        return cls.model.objects.filter(**kwargs).first()


class BaseSyncRepository(BaseRepository):
    @staticmethod
    def sync_update_queryset(queryset, **values):
        """Update a small synchronized set without bypassing SyncMixin.save().

        This is intentionally opt-in, not a QuerySet.update override: SQL bulk
        updates remain available for local bookkeeping tables and deliberate
        atomic F() operations. Catalog/stock callers use this when every
        changed row must increment sync_version and enter the change pipeline.
        """
        from django.db import transaction

        fields = list(values)
        changed = 0
        with transaction.atomic():
            for instance in queryset.select_for_update():
                dirty = False
                for field, value in values.items():
                    if getattr(instance, field) != value:
                        setattr(instance, field, value)
                        dirty = True
                if dirty:
                    instance.save(update_fields=fields)
                    changed += 1
        return changed

    @classmethod
    def get_by_id(cls, pk):
        try:
            return cls.model.objects.get(pk=pk, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_uuid(cls, uuid_val):
        try:
            return cls.model.objects.get(uuid=uuid_val, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_all(cls):
        return cls.model.objects.filter(is_deleted=False)

    @classmethod
    def filter(cls, **kwargs):
        return cls.model.objects.filter(is_deleted=False, **kwargs)

    @classmethod
    def count(cls, **kwargs):
        return cls.model.objects.filter(is_deleted=False, **kwargs).count()

    @classmethod
    def exists(cls, **kwargs):
        return cls.model.objects.filter(is_deleted=False, **kwargs).exists()

    @classmethod
    def first(cls, **kwargs):
        return cls.model.objects.filter(is_deleted=False, **kwargs).first()

    @classmethod
    def hard_delete(cls, instance):
        instance.hard_delete()

    @classmethod
    def get_unsynced(cls):
        return cls.model.objects.unsynced()

    @classmethod
    def get_by_branch(cls, branch_id):
        return cls.model.objects.filter(is_deleted=False, branch_id=branch_id)
