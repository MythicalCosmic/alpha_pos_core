from base.models import AppSettings


class AppSettingsRepository:
    @classmethod
    def load(cls):
        return AppSettings.load()
