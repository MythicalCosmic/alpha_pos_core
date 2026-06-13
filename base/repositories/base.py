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
