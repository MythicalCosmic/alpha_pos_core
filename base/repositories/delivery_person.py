from base.repositories.base import BaseSyncRepository
from base.models import DeliveryPerson


class DeliveryPersonRepository(BaseSyncRepository):
    model = DeliveryPerson

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_phone(cls, phone_number):
        try:
            return cls.model.objects.get(
                phone_number=phone_number,
                is_deleted=False,
            )
        except cls.model.DoesNotExist:
            return None
