from base.repositories.base import BaseSyncRepository
from stock.models import StockSettings, StockAlertConfig


class StockSettingsRepository(BaseSyncRepository):
    model = StockSettings

    @classmethod
    def load(cls):
        obj, _ = cls.model.objects.get_or_create(pk=1)
        return obj


class StockAlertConfigRepository(BaseSyncRepository):
    model = StockAlertConfig

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(is_deleted=False, is_active=True)

    @classmethod
    def get_by_type(cls, alert_type):
        return cls.model.objects.filter(
            alert_type=alert_type, is_deleted=False
        ).first()
